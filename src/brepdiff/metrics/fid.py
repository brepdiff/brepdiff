"""Calculates the Frechet Inception Distance (FID) to evalulate GANs

The FID metric calculates the distance between two distributions of images.
Typically, we have summary statistics (mean & covariance matrix) of one
of these distributions, while the 2nd distribution is given by a GAN.

When run as a stand-alone program, it compares the distribution of
images that are stored as PNG/JPEG at a specified location with a
distribution given by summary statistics (in pickle format).

The FID is calculated by assuming that X_1 and X_2 are the activations of
the pool_3 layer of the inception net for generated samples and real world
samples respectively.

See --help to see further details.

Code apapted from https://github.com/bioinf-jku/TTUR to use PyTorch instead
of Tensorflow

Copyright 2018 Institute of Bioinformatics, JKU Linz

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import os
import pathlib
from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser

import numpy as np
import torch
import torchvision.transforms as TF
from PIL import Image
from scipy import linalg
from torch.nn.functional import adaptive_avg_pool2d

try:
    from tqdm import tqdm
except ImportError:
    # If tqdm is not available, provide a mock version of it
    def tqdm(x):
        return x


from brepdiff.metrics.resnet import load_resnet
from brepdiff.metrics.inception import InceptionV3


class ImagePathDataset(torch.utils.data.Dataset):
    def __init__(self, files, transforms=None):
        self.files = files
        self.transforms = transforms

    def __len__(self):
        return len(self.files)

    def __getitem__(self, i):
        path = self.files[i]
        img = Image.open(path).convert("RGB")
        if self.transforms is not None:
            img = self.transforms(img)
        return img


def get_activations(
    files,
    model,
    normalize_rgb: bool,
    batch_size=50,
    device="cpu",
    num_workers=1,
    # Make sure to resize
    target_size: int = 256,
    verbose: bool = True,
):
    """Calculates the activations of the pool_3 layer for all images.

    Params:
    -- files       : List of image files paths
    -- model       : Instance of inception model
    -- batch_size  : Batch size of images for the model to process at once.
                     Make sure that the number of samples is a multiple of
                     the batch size, otherwise some samples are ignored. This
                     behavior is retained to match the original FID score
                     implementation.
    -- dims        : Dimensionality of features returned by Inception
    -- device      : Device to run calculations
    -- num_workers : Number of parallel dataloader workers
    -- normalize_rgb: normalize RGB channels as done in pretrained resnet

    Returns:
    -- A numpy array of dimension (num images, dims) that contains the
       activations of the given tensor when feeding inception with the
       query tensor.
    """
    model.eval()

    if batch_size > len(files):
        print(
            (
                "Warning: batch size is bigger than the data size. "
                "Setting batch size to data size"
            )
        )
        batch_size = len(files)

    transforms = [TF.ToTensor(), TF.Resize((target_size))]
    if normalize_rgb:
        transforms.append(
            TF.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        )
    dataset = ImagePathDataset(files, transforms=TF.Compose(transforms))

    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        drop_last=False,
        num_workers=num_workers,
    )

    pred_arr = []

    for batch in tqdm(dataloader, disable=not verbose):
        batch = batch.to(device)

        with torch.no_grad():
            pred = model(batch)[0]

        # If model output is not scalar, apply global spatial average pooling.
        # This happens if you choose a dimensionality not equal 2048.
        if pred.size(2) != 1 or pred.size(3) != 1:
            pred = adaptive_avg_pool2d(pred, output_size=(1, 1))

        pred = pred.squeeze(3).squeeze(2).cpu().numpy()

        pred_arr.append(pred)

    pred_arr = np.concatenate(pred_arr, axis=0)

    return pred_arr


def calculate_frechet_distance(mu1, sigma1, mu2, sigma2, eps=1e-6):
    """Numpy implementation of the Frechet Distance.
    The Frechet distance between two multivariate Gaussians X_1 ~ N(mu_1, C_1)
    and X_2 ~ N(mu_2, C_2) is
            d^2 = ||mu_1 - mu_2||^2 + Tr(C_1 + C_2 - 2*sqrt(C_1*C_2)).

    Stable version by Dougal J. Sutherland.

    Params:
    -- mu1   : Numpy array containing the activations of a layer of the
               inception net (like returned by the function 'get_predictions')
               for generated samples.
    -- mu2   : The sample mean over activations, precalculated on an
               representative data set.
    -- sigma1: The covariance matrix over activations for generated samples.
    -- sigma2: The covariance matrix over activations, precalculated on an
               representative data set.

    Returns:
    --   : The Frechet Distance.
    """

    mu1 = np.atleast_1d(mu1)
    mu2 = np.atleast_1d(mu2)

    sigma1 = np.atleast_2d(sigma1)
    sigma2 = np.atleast_2d(sigma2)

    assert (
        mu1.shape == mu2.shape
    ), "Training and test mean vectors have different lengths"
    assert (
        sigma1.shape == sigma2.shape
    ), "Training and test covariances have different dimensions"

    diff = mu1 - mu2

    # Product might be almost singular
    covmean, _ = linalg.sqrtm(sigma1.dot(sigma2), disp=False)
    if not np.isfinite(covmean).all():
        msg = (
            "fid calculation produces singular product; "
            "adding %s to diagonal of cov estimates"
        ) % eps
        print(msg)
        offset = np.eye(sigma1.shape[0]) * eps
        covmean = linalg.sqrtm((sigma1 + offset).dot(sigma2 + offset))

    # Numerical error might give slight imaginary component
    if np.iscomplexobj(covmean):
        if not np.allclose(np.diagonal(covmean).imag, 0, atol=1e-3):
            m = np.max(np.abs(covmean.imag))
            raise ValueError("Imaginary component {}".format(m))
        covmean = covmean.real

    tr_covmean = np.trace(covmean)

    return diff.dot(diff) + np.trace(sigma1) + np.trace(sigma2) - 2 * tr_covmean


def calculate_activation_statistics(
    files,
    model,
    normalize_rgb: bool,
    batch_size=50,
    device="cpu",
    num_workers=1,
    verbose: bool = True,
):
    """Calculation of the statistics used by the FID.
    Params:
    -- files       : List of image files paths
    -- model       : Instance of inception model
    -- batch_size  : The images numpy array is split into batches with
                     batch size batch_size. A reasonable batch size
                     depends on the hardware.
    -- dims        : Dimensionality of features returned by Inception
    -- device      : Device to run calculations
    -- num_workers : Number of parallel dataloader workers
    -- normalize_rgb: normalize RGB channels as done in pretrained resnet

    Returns:
    -- mu    : The mean over samples of the activations of the pool_3 layer of
               the inception model.
    -- sigma : The covariance matrix of the activations of the pool_3 layer of
               the inception model.
    """
    act = get_activations(
        files,
        model,
        normalize_rgb,
        batch_size,
        device,
        num_workers,
        verbose=verbose,
    )
    mu = np.mean(act, axis=0)
    sigma = np.cov(act, rowvar=False)
    return mu, sigma


def compute_statistics_of_paths(
    paths,
    model,
    normalize_rgb: bool,
    batch_size,
    device,
    num_workers=1,
    verbose: bool = True,
):
    m, s = calculate_activation_statistics(
        paths,
        model,
        normalize_rgb,
        batch_size,
        device,
        num_workers,
        verbose=verbose,
    )

    return m, s


def calculate_fid_given_paths(
    paths, batch_size, device, fid_type: str = "sketch", num_workers=1
):
    """Calculates the FID of two list of paths"""

    if fid_type == "sketch":
        # WARNING: this is hardcoded
        model = load_resnet("data/resnet18_sketch", [3]).to(device)
        normalize_rgb = False
    elif fid_type == "cad":
        model = load_resnet("data/abc_processed/fid/resnet18_cad.ckpt", [3]).to(device)
        normalize_rgb = True
    elif fid_type == "cad_v1":
        model = load_resnet("data/abc_processed/fid/resnet18_cad_v1.ckpt", [3]).to(
            device
        )
        normalize_rgb = True
    elif fid_type == "vanilla":
        model = InceptionV3([3]).to(device)
        normalize_rgb = False
    else:
        raise NotImplementedError(f"{fid_type} isn't a valid FID type")

    m1, s1 = compute_statistics_of_paths(
        paths[0], model, normalize_rgb, batch_size, device, num_workers
    )
    m2, s2 = compute_statistics_of_paths(
        paths[1],
        model,
        normalize_rgb,
        batch_size,
        device,
        num_workers,
    )
    fid_value = calculate_frechet_distance(m1, s1, m2, s2)

    return fid_value


def calculate_fid_given_npz_and_paths(
    npz_path, paths, batch_size, device="cuda", fid_type: str = "sketch", num_workers=1
):
    """Calculates the FID of two list of paths"""

    if fid_type == "sketch":
        # WARNING: this is hardcoded
        model = load_resnet("data/resnet18_sketch", [3]).to(device)
        normalize_rgb = False
    elif fid_type == "cad":
        model = load_resnet("data/abc_processed/fid/resnet18_cad.ckpt", [3], 40).to(
            device
        )
        normalize_rgb = True
    elif fid_type == "cad_v1":
        model = load_resnet("data/abc_processed/fid/resnet18_cad_v1.ckpt", [3], 40).to(
            device
        )
        normalize_rgb = True
    elif fid_type == "vanilla":
        model = InceptionV3([3]).to(device)
        normalize_rgb = False
    else:
        raise NotImplementedError(f"{fid_type} isn't a valid FID type")

    cached_fid = np.load(npz_path)
    m1, s1 = cached_fid["mu"], cached_fid["sigma"]
    m2, s2 = compute_statistics_of_paths(
        paths, model, normalize_rgb, batch_size, device, num_workers, verbose=False
    )
    fid_value = calculate_frechet_distance(m1, s1, m2, s2)

    return fid_value


def save_fid_stats(
    paths, output, batch_size, device, fid_type: str = "sketch", num_workers=1
):
    """Saves FID statistics of one path"""

    if fid_type == "sketch":
        # WARNING: this is hardcoded
        model = load_resnet("data/resnet18_sketch", [3]).to(device)
        normalize_rgb = False
    elif fid_type == "cad":
        model = load_resnet("data/abc_processed/fid/resnet18_cad.ckpt", [3], 40).to(
            device
        )
        normalize_rgb = True
    elif fid_type == "cad_v1":
        model = load_resnet("data/abc_processed/fid/resnet18_cad_v1.ckpt", [3], 40).to(
            device
        )
        normalize_rgb = True
    elif fid_type == "vanilla":
        model = InceptionV3([3]).to(device)
        normalize_rgb = False
    else:
        raise NotImplementedError(f"{fid_type} isn't a valid FID type")

    m1, s1 = compute_statistics_of_paths(
        paths, model, normalize_rgb, batch_size, device, num_workers
    )

    print(f"Saving FID stats to {output}")
    np.savez_compressed(output, mu=m1, sigma=s1)
