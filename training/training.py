import os
import sys
import yaml
import time
import shutil
import torch
import random
import argparse
import datetime
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
import scipy.misc as misc
import numpy as np
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms, utils
from torch.utils import data
from tqdm import tqdm
from rootnav2.hourglass import hg
import cv2
from rootnav2.loss import get_loss_function
from rootnav2.loader import get_loader 
from rootnav2.utils import get_logger
from rootnav2.metrics import runningScore, averageMeter
from rootnav2.schedulers import get_scheduler
from rootnav2.optimizers import get_optimizer
from pathlib import Path
from publish import publish

# Class weights
weights = [0.0021,0.1861,2.3898,0.6323,28.6333,31.0194]

def train(args):
    # Load Config
    with open(args.config) as fp:
        cfg = yaml.load(fp)

    # Create log and output directory
    run_id = random.randint(1,100000)
    logdir = os.path.join('runs', os.path.basename(args.config)[:-4] , str(run_id))
    #writer = SummaryWriter(log_dir=logdir)

    print('RUNDIR: {}'.format(logdir))

    if not os.path.exists(logdir):
        os.makedirs(logdir)

    shutil.copy(args.config, logdir)

    logger = get_logger(logdir)
    logger.info('Starting training')

    # Setup device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    # Class weights
    class_weights = torch.FloatTensor(weights).to(device)

    # Setup seeds
    torch.manual_seed(cfg.get('seed', 1337))
    torch.cuda.manual_seed(cfg.get('seed', 1337))
    np.random.seed(cfg.get('seed', 1337))
    random.seed(cfg.get('seed', 1337))

    # Is hflip in use?
    augmentations = cfg['training'].get('augmentations', 0.0)
    if augmentations is not None:
        hflip = augmentations.get('hflip', 0.0)
    else:
        hflip = 0.0

    # Setup Dataloader
    data_loader = get_loader(cfg['data']['dataset'])
    data_path = cfg['data']['path']

    print ("Dataset Loading from...", data_path)

    t_loader = data_loader(
        data_path,
        split='train',
        hflip=hflip)

    v_loader = data_loader(
        data_path,
        split='valid')

    n_classes = t_loader.n_classes
    trainloader = data.DataLoader(t_loader,
                                  batch_size=cfg['training']['batch_size'], 
                                  num_workers=1,#cfg['training']['n_workers'], 
                                  shuffle=True)

    valloader = data.DataLoader(v_loader, 
                                batch_size=cfg['training']['batch_size'], 
                                num_workers=cfg['training']['n_workers'])

    # Setup Metrics
    running_metrics_val = runningScore(n_classes)

    # Setup Model
    model =  hg()

    model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))
    model.to(device)
    # Setup optimizer, lr_scheduler and loss function
    optimizer_cls = get_optimizer(cfg)
    optimizer_params = {k:v for k, v in cfg['training']['optimizer'].items() 
                        if k != 'name'}

    optimizer = optimizer_cls(model.parameters(), **optimizer_params)
    logger.info("Using optimizer {}".format(optimizer))

    scheduler = get_scheduler(optimizer, cfg['training']['lr_schedule'])

    loss_fn = get_loss_function(cfg)
    logger.info("Using loss {}".format(loss_fn))

    start_iter = 0
    if cfg['training']['resume'] is not None:
        if os.path.isfile(cfg['training']['resume']):
            logger.info(
                "Loading model and optimizer from checkpoint '{}'".format(cfg['training']['resume'])
            )
            checkpoint = torch.load(cfg['training']['resume'])
            model.load_state_dict(checkpoint["model_state"])
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            scheduler.load_state_dict(checkpoint["scheduler_state"])
            start_iter = checkpoint["epoch"]
            logger.info(
                "Loaded checkpoint '{}' (iter {})".format(
                    cfg['training']['resume'], checkpoint["epoch"]
                )
            )
        else:
            logger.info("No checkpoint found at '{}'".format(cfg['training']['resume']))

    val_loss_meter = averageMeter()
    time_meter = averageMeter()

    best_iou = -100.0
    i = start_iter
    flag = True
    bce_criterion = torch.nn.CrossEntropyLoss(weight=class_weights).to(device)
    mse_criterion = torch.nn.MSELoss(size_average=True).to(device)

    while i <= cfg['training']['train_iters'] and flag:
        for (images, labels, hm) in trainloader:

            # LIST OF network OUTPUTS
            # seg = outputs[0] : Batch x 3 x 512 x 512
            # reg = outputs[1] : Batch x 3 x 512 x 512
            #
            # LIST OF Loader OUTPUTS
            # labels from loader : Batch x 1 x 512 x 512
            # 0 = BG, 1 = PRI, 2 = LAT
            #
            # hm from loader: Batch x 3 x 512 x 512
            i += 1
            start_ts = time.time()
            scheduler.step()
            model.train()
            images = images.to(device)
            labels = labels.to(device)
            hm = hm.to(device)

            outputs= model(images)
            out_main= outputs[-1]
            sys.stdout.flush()
            
            optimizer.zero_grad()
            
            loss1 = bce_criterion(input=out_main, target=labels)

            out5= out_main[:,5:6,:,:] 
            out4= out_main[:,4:5,:,:]
            out2= out_main[:,2:3,:,:] 

            tips = torch.cat((out2, out4,  out5), 1)
            loss2 = mse_criterion(input=tips, target=hm)

            loss1.backward(retain_graph=True)
            loss2.backward()

            optimizer.step()

            time_meter.update(time.time() - start_ts)

            if (i + 1) % cfg['training']['print_interval'] == 0:
                fmt_str = "Iter [{:d}/{:d}]  Loss: {:.4f}  Time/Image: {:.4f}"
                print_str = fmt_str.format(i + 1,
                                           cfg['training']['train_iters'], 
                                           loss1.item(),
                                           time_meter.avg / cfg['training']['batch_size'])

                print(print_str)
                logger.info(print_str)
                #writer.add_scalar('loss/train_loss', loss1.item(), i+1)
                time_meter.reset()

            if (i + 1) % cfg['training']['val_interval'] == 0 or \
               (i + 1) == cfg['training']['train_iters']:
                model.eval()
                with torch.no_grad():
                    for i_val, (images_val, labels_val, hm) in tqdm(enumerate(valloader)):
                        images_val = images_val.to(device)
                        labels_val = labels_val.to(device)
                        
                        outputs = model(images_val)
                        outputs1= outputs[-1]
                        
                        val_loss1 = bce_criterion(input=outputs1, target=labels_val)
                        
                        pred = outputs1.data.max(1)[1].cpu().numpy()
                        pred1 = np.squeeze(outputs1[0:1,:,:,:].data.max(1)[1].cpu().numpy(), axis=0)
                        gt = labels_val.data.cpu().numpy()

                        running_metrics_val.update(gt, pred)
                        val_loss_meter.update(val_loss1.item())


                #writer.add_scalar('loss/val_loss', val_loss_meter.avg, i+1)
                logger.info("Iter %d Loss: %.4f" % (i + 1, val_loss_meter.avg))

                score, class_iou = running_metrics_val.get_scores()
                for k, v in score.items():
                    print(k, v)
                    logger.info('{}: {}'.format(k, v))
                    #.add_scalar('val_metrics/{}'.format(k), v, i+1)

                for k, v in class_iou.items():
                    logger.info('{}: {}'.format(k, v))
                    writer.add_scalar('val_metrics/cls_{}'.format(k), v, i+1)

                val_loss_meter.reset()
                running_metrics_val.reset()
                #####################picture ##################              
                decoded = v_loader.decode_segmap(pred1)              
                #############################################  
                out_path = 'snapshot.jpg'
                misc.imsave(out_path, decoded)
                #############################################

                if score["Mean IoU : \t"] >= best_iou:
                    best_iou = score["Mean IoU : \t"]
                    state = {
                        "epoch": i + 1,
                        "model_state": model.state_dict(),
                        "optimizer_state": optimizer.state_dict(),
                        "scheduler_state": scheduler.state_dict(),
                        "best_iou": best_iou,
                    }
                    #save_path = os.path.join(writer.file_writer.get_logdir(),
                    #                         "{}_{}_best_model.pkl".format(
                    #                             cfg['model']['arch'],
                    #                             cfg['data']['dataset']))
                    #torch.save(state, save_path)

            if (i + 1) == cfg['training']['train_iters']:
                flag = False
                break

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RootNav 2 Training")
    subparsers = parser.add_subparsers(title="Mode")

    # Train sub command
    parser_train = subparsers.add_parser('train', help='Train new models')
    parser_train.add_argument("--config", nargs="?", type=str, default="configs/rootnav2.yml", help="Configuration file to use")
    parser_train.set_defaults(func=train)

    # Publish sub command
    parser_publish = subparsers.add_parser('publish', help='Publish already trained models')
    parser_publish.add_argument('--name', default="published_model", metavar='N', help="The name of the new published model")
    parser_publish.add_argument('--parent', default=None, metavar='P', help="The name of the parent model used to begin training")
    parser_publish.add_argument('--model', metavar='M', help="The trained weights file to publish")
    parser_publish.add_argument('--multi-plant', action='store_true', help="Whether or not images are expected to contain multiple plants")
    parser_publish.add_argument('--use-parent-config', action='store_true', help="Whether or not to use the parent pathing and network configuration, or to use default values")
    parser_publish.add_argument('output_dir', default='./', type=str, help='Output directory')
    parser_publish.set_defaults(func=publish)

    args = parser.parse_args()
    args.func(args)
