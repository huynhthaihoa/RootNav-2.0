# RootNav 2.0 - Python 2.6 version
This is the Python 2.6 version of the RootNav 2.0 Code repository. This version was used to produce the results in our submitted paper, but ongoing development has now switched to Python 3.6. We will be trying to keep the functionality the same in both branches. We have found that the requisite libraries are more widely available on different platforms in Python 3, and so have made this the default from now on.

### Environment
RootNav 2.0 requires specific libraries in order to run. These include pytorch 1.0.1, torchvision, numpy, yaml and so on. Those who are knowledgeable in python and installing libraries will find it easy to simply run the code, identify missing libraries and install them via pip. For convenience, the included requirements.txt, we found support for the libraries is best within Linux.

`pip install -r requirements.txt`

If you require RootNav 2.0 support in Windows or another operating system, we recommend using the version compatible with Python 3.6.

### Using the tool
The inference folder is what people who are looking to use RootNav 2.0 require. All code necessary to train new models is included in the training folder.

## Training
Training code may be found in the training folder, please refer to the documentation there and in the Python 3 repository.

## Inference
Running the tool using pre-trained models can be done within the inference folder. Three examples are currently provided for the three datasets. You can find out what models are currently supported using this command:

```python rootnav.py --list```

You can then run the tool using:

```python rootnav.py --model <modelname> input_dir output_dir```

Rootnav 2.0 will find all images within the input directory, process them as per the model, and output files into the output directory.
