"""
Dataset Import/Download Tools
"""

import os
import random
import numpy as np
import rawpy
from PIL import Image

import torch

from skimage.util.shape import view_as_windows

IMAGE_FILE_TYPES = ['dng', 'png', 'tif', 'tiff']

def load_image(path):
    file_type = path.split('.')[-1].lower()
    if file_type == 'dng':
        img = rawpy.imread(path).raw_image_visible
    elif file_type == 'tiff' or file_type == 'tif':
        img = np.array(tiff.imread(path), dtype=np.float32)
    else:
        img = np.array(Image.open(path), dtype=np.float32)
    return img


def list_images_in_dir(path):
    image_list = [os.path.join(path, img_name)
                  for img_name in sorted(os.listdir(path))
                  if img_name.split('.')[-1].lower() in IMAGE_FILE_TYPES]
    return image_list


def k_fold(dataset, n_splits: int, seed: int, train_size: float):
    """Split dataset in subsets for cross-validation 

       Args:
            dataset (class): dataset to split
            n_split (int): Number of re-shuffling & splitting iterations.
            seed (int): seed for k_fold splitting
            train_size (float): should be between 0.0 and 1.0 and represent the proportion of the dataset to include in the train split.
       Returns:
           idxs (list): indeces for splitting the dataset. The list contain n_split pair of train/test indeces.
    """
    if hasattr(dataset, 'labels'):
        x = dataset.images
        y = dataset.labels
    elif hasattr(dataset, 'masks'):
        x = dataset.images
        y = dataset.masks

    idxs = []

    if dataset.task == 'classification':
        sss = StratifiedShuffleSplit(n_splits=n_splits, train_size=train_size, random_state=seed)

        for idxs_train, idxs_test in sss.split(x, y):
            idxs.append((idxs_train.tolist(), idxs_test.tolist()))

    elif dataset.task == 'segmentation':
        for n in range(n_splits):
            split_idx = int(len(dataset) * train_size)
            indices = np.random.permutation(len(dataset))
            idxs.append((indices[:split_idx].tolist(), indices[split_idx:].tolist()))

    return idxs


def split_img(imgs, ROIs=(3, 3), step=(1, 1)):
    """Split the imgs in regions of size ROIs.

       Args:
            imgs (ndarray): images which you want to split 
            ROIs (tuple): size of sub-regions splitted (ROIs=region of interests)
            step (tuple): step path from one sub-region to the next one (in the x,y axis)

        Returns:
            ndarray: splitted subimages. 
                     The size is (x_num_subROIs*y_num_subROIs, **) where:
                     x_num_subROIs = ( imgs.shape[1]-int(ROIs[1]/2)*2 )/step[1]
                     y_num_subROIs = ( imgs.shape[0]-int(ROIs[0]/2)*2 )/step[0]                     

       Example:
            >>> from dataset_generator import split
            >>> imgs_splitted = split(imgs, ROI_size = (5,5), step=(2,3))
    """

    if len(ROIs) > 2:
        return print("ROIs is a 2 element list")

    if len(step) > 2:
        return print("step is a 2 element list")

    if type(imgs) != type(np.array(1)):
        return print("imgs should be a ndarray")

    if len(imgs.shape) == 2:  # Single image with one channel (HxW)
        splitted = view_as_windows(imgs, (ROIs[0], ROIs[1]), (step[0], step[1]))
        return splitted.reshape((-1, ROIs[0], ROIs[1]))

    if len(imgs.shape) == 3:
        _, _, channels = imgs.shape
        if channels <= 3:  # Single image more channels (HxWxC)
            splitted = view_as_windows(imgs, (ROIs[0], ROIs[1], channels), (step[0], step[1], channels))
            return splitted.reshape((-1, ROIs[0], ROIs[1], channels))
        else:  # More images with 1 channel
            splitted = view_as_windows(imgs, (1, ROIs[0], ROIs[1]), (1, step[0], step[1]))
            return splitted.reshape((-1, ROIs[0], ROIs[1]))

    if len(imgs.shape) == 4:  # More images with more channels(BxHxWxC)
        _, _, _, channels = imgs.shape
        splitted = view_as_windows(imgs, (1, ROIs[0], ROIs[1], channels), (1, step[0], step[1], channels))
        return splitted.reshape((-1, ROIs[0], ROIs[1], channels))


def join_blocks(splitted, final_shape):
    """Join blocks to reobtain a splitted image

    Attribute:
        splitted (tensor) = image splitted in blocks, size = (N_blocks, Channels, Height, Width)
        final_shape (tuple) = size of the final image reconstructed (Height, Width)
    Return:
        tensor: image restored from blocks. size = (Channels, Height, Width)

    """
    n_blocks, channels, ROI_height, ROI_width = splitted.shape

    rows = final_shape[0] // ROI_height
    columns = final_shape[1] // ROI_width

    final_img = torch.empty(rows, channels, ROI_height, ROI_width * columns)
    for r in range(rows):
        stackblocks = splitted[r * columns]
        for c in range(1, columns):
            stackblocks = torch.cat((stackblocks, splitted[r * columns + c]), axis=2)
        final_img[r] = stackblocks

    joined_img = final_img[0]

    for i in np.arange(1, len(final_img)):
        joined_img = torch.cat((joined_img, final_img[i]), axis=1)

    return joined_img


def random_ROI(X, Y, ROIs=(512, 512)):
    """ Return a random region for each input/target pair images of the dataset
        Args:
            Y (ndarray): target of your dataset --> size: (BxHxWxC)
            X (ndarray): input of your dataset --> size: (BxHxWxC)
            ROIs (tuple): size of random region (ROIs=region of interests)

        Returns:
            For each pair images (input/target) of the dataset, return respectively random ROIs
            Y_cut (ndarray): target of your dataset --> size: (Batch,Channels,ROIs[0],ROIs[1])
            X_cut (ndarray): input of your dataset --> size: (Batch,Channels,ROIs[0],ROIs[1])

        Example:
            >>> from dataset_generator import random_ROI
            >>> X,Y = random_ROI(X,Y, ROIs = (10,10))
    """

    batch, channels, height, width = X.shape

    X_cut = np.empty((batch, ROIs[0], ROIs[1], channels))
    Y_cut = np.empty((batch, ROIs[0], ROIs[1], channels))

    for i in np.arange(len(X)):
        x_size = int(random.random() * (height - (ROIs[0] + 1)))
        y_size = int(random.random() * (width - (ROIs[1] + 1)))
        X_cut[i] = X[i, x_size:x_size + ROIs[0], y_size:y_size + ROIs[1], :]
        Y_cut[i] = Y[i, x_size:x_size + ROIs[0], y_size:y_size + ROIs[1], :]
    return X_cut, Y_cut


def one2many_random_ROI(X, Y, datasize=1000, ROIs=(512, 512)):
    """ Return a dataset of N subimages obtained from random regions of the same image
        Args:
            Y (ndarray): target of your dataset --> size: (1,H,W,C)
            X (ndarray): input of your dataset --> size: (1,H,W,C)
            datasize = number of random ROIs to generate
            ROIs (tuple): size of random region (ROIs=region of interests)

        Returns:
            Y_cut (ndarray): target of your dataset --> size: (Datasize,ROIs[0],ROIs[1],Channels)
            X_cut (ndarray): input of your dataset --> size: (Datasize,ROIs[0],ROIs[1],Channels)
    """

    batch, channels, height, width = X.shape

    X_cut = np.empty((datasize, ROIs[0], ROIs[1], channels))
    Y_cut = np.empty((datasize, ROIs[0], ROIs[1], channels))

    for i in np.arange(datasize):
        X_cut[i], Y_cut[i] = random_ROI(X, Y, ROIs)
    return X_cut, Y_cut
