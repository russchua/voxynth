import numpy as np
import torch

from torch import Tensor
from typing import List, Tuple

from .utility import chance
from .utility import grid_coordinates
from .filter import gaussian_blur
from .noise import perlin


def image_augment(
    image: Tensor,
    mask: Tensor = None,
    voxsize: List[float] = 1.0,
    normalize_input: bool = True,
    smoothing_probability: float = 0.0,
    smoothing_one_axis_probability: float = 0.5,
    smoothing_max_sigma: float = 2.0,
    bias_field_probability: float = 0.0,
    bias_field_max_strength: float = 0.5,
    bias_field_wavelength_range: Tuple[float, float] = [20, 60],
    background_noise_probability: float = 0.0,
    background_blob_probability: float = 0.0,
    background_roll_probability: float = 0.0,
    background_roll_max_strength: float = 0.5,
    added_noise_probability: float = 0.0,
    added_noise_max_sigma: float = 0.05,
    wave_artifact_probability: float = 0.0,
    wave_artifact_max_strength: float = 0.05,
    line_corruption_probability: float = 0.0,
    gamma_scaling_probability: float = 0.0,
    gamma_scaling_max: float = 0.8,
    resized_probability: float = 0.0,
    resized_one_axis_probability: float = 0.5,
    resized_max_voxsize: float = 2):
    """
    Augment an image with a variety of random effects.

    Supports intensity smoothing, bias field simulation, background noise synthesis,
    a variety of corruptions, and resolution scaling. This function is designed to be
    used with a channeled image tensor, where the first dimension is the channel dimension.
    Augmentation is applies independently to across seperate channels.

    A boolean mask can be optionally provided. Any image signal outside of the mask is
    considered background and replaced by synthetic noise and/or other effects.

    Note that this function expects the image singals to be between 0 and 1, so it will
    min/max normalize the input by default. This can be disabled with the `normalize_input`
    parameter.

    Parameters
    ----------
    image: Tensor
        An image tensor of shape `(channels, *shape)`. Can be 2D or 3D.
    mask: Tensor, optionals
        An mask tensor of shape `shape`.
    voxsize: float or List[float], optional
        The relative size of the voxel. This is used to appropriately scale
        spatial-based parameters.
    normalize_input: bool, optional
        If True, the image is min/max normalized before augmentation. Not necessary if the
        image intensities are already between 0 and 1.
    smoothing_probability: float, optional
        The probability of applying a gaussian smoothing kernel.
    smoothing_one_axis_probability: float, optional
        The probability of applying the smoothing kernel to a single axis. This
        is a sub-probability of the `smoothing_probability`.
    smoothing_max_sigma: float, optional
        The maximum sigma for the smoothing kernel.
    bias_field_probability: float, optional
        The probability of simulating a bias field.
    bias_field_max_strength: float, optional
        The maximum strength of the bias field.
    bias_field_wavelength_range: Tuple, optional
        The range of perlin noise wavelengths to generate the bias field.
    background_noise_probability: float, optional
        The probability of synthesizing perline noise in the background. Otherwise,
        the background will be set to zero.
    background_blob_probability: float, optional
        The probability of adding random blobs of noise to the background.
    background_roll_probability: float, optional
        The probability of rolling the image around the background.
    background_roll_max_strength: float, optional
        The maximum scale for rolling the image around the background.
    added_noise_probability: float, optional
        The probability of adding random Gaussian noise across the entire image.
    added_noise_max_sigma: float, optional
        The maximum sigma for the added Gaussian noise.
    wave_artifact_probability: float, optional
        The probability of adding wave artifacts or grating effects to the image.
    wave_artifact_max_strength: float, optional
        The maximum strength (intensity) of added wave artifacts.
    line_corruption_probability: float, optional
        The probability of adding random line artifacts to the image, i.e. blocking out
        signal in a random slice.
    gamma_scaling_probability: float, optional
        The probability of scaling the image intensities with a gamma function.
    gamma_scaling_max: float, optional
        The maximum value for the gamma exponentiation.
    resized_probability: float, optional
        The probability of downsampling, then re-upsampling the image to synthesize
        low-resolution image resizing.e
    resized_one_axis_probability: float, optional
        The probability of resizing only one axis, to simulate thick-slice data.
    resized_max_voxsize: float, optional
        The maximum voxel size for the 'resized' downsampling step.

    Returns
    -------
    Tensor
        The augmented image tensor of shape (channels, *shape).
    """

    # parse some info about the input image
    device = image.device
    shape = image.shape[1:]
    channels = image.shape[0]
    ndim = len(shape)

    # voxsize is used to scale any parameters that spatially depend on the image resolution
    voxsize = torch.as_tensor(voxsize, device=device)
    if voxsize.ndim == 0:
        voxsize = voxsize.repeat(ndim)

    # convert to float32 if necessary
    image = image.clone() if torch.is_floating_point(image) else image.type(torch.float32)

    # min/max normalize since everything below operates with the
    # assumption that intensities are between 0 and 1
    if normalize_input:
        dims = tuple([i + 1 for i in range(ndim)])
        image -= image.amin(dim=dims, keepdim=True)
        image /= image.amax(dim=dims, keepdim=True)
    elif image.min() < 0 or image.max() > 1:
        raise ValueError('image intensities must be between 0 and 1')

    # mask, if provided, should be a boolean tensor of the same base shape (no channel dim)
    if mask is None:
        mask = image.sum(0) > 0
    elif mask.ndim != (image.ndim - 1):
        raise ValueError(f'expected mask to have {ndim} dims, but got shape {mask.shape}')

    # cache for background data augmentation
    background = mask == 0

    # each channel should be processed as independent images
    for channel in range(channels):

        # grab the current channel
        cimg = image[channel]

        # ---- intensity smoothing ----

        # apply a random gaussian smoothing kernel
        if chance(smoothing_probability):
            max_sigma = (smoothing_max_sigma / voxsize.min()).cpu().numpy()  # TODO: just use torch
            if chance(smoothing_one_axis_probability):
                # here we only smooth one axis to emulate thick slice data
                sigma = np.zeros(ndim)
                sigma[np.random.randint(ndim)] = np.random.uniform(0, max_sigma)
            else:
                # otherwise just smooth in all dimensions
                sigma = np.random.uniform(0, max_sigma, size=ndim)
            cimg = gaussian_blur(cimg.unsqueeze(0), sigma)
    
        # ---- background synthesis ----

        if chance(background_noise_probability):
            # set the background as perline noise with a random mean
            wavelength = torch.ceil(torch.tensor(np.random.uniform(1, 16)) / voxsize)
            bg_image = perlin(shape, wavelength, device=device)
            bg_image /= np.random.uniform(1, 10)
            bg_image += np.random.rand()
        else:
            # otherwise just set it to zero
            bg_image = torch.zeros(shape, device=device)

        # add random blobs of noise to the background
        if chance(background_blob_probability):
            wavelength = torch.ceil(torch.tensor(np.random.uniform(32, 64)) / voxsize)
            noise = perlin(shape, wavelength, device=device)
            blobs = noise > np.random.uniform(-0.2, 0.2)
            bg_image[blobs] = np.random.rand() if chance(0.5) else noise[blobs] * np.random.rand()

        # here we copy-paste parts of the image around the background via axis rolling
        if chance(background_roll_probability):
            for i in range(np.random.randint(1, 4)):
                dims = tuple(np.random.permutation(ndim)[:np.random.choice((1, 2))])
                shifts = [int(np.random.uniform(shape[d] / 4, shape[d] / 2)) for d in dims]
                shifts = tuple(np.asarray(shifts) * np.random.choice([-1, 1], size=len(shifts)))
                scale = np.random.randn() * background_roll_max_strength
                bg_image += scale * torch.roll(cimg, shifts, dims=dims)

        # finally we copy the background into the image
        cimg[background] = bg_image[background].clip(0, 1)

        # ---- corruptions ----

        # synthesize a bias field of varying degrees of wavelength and intensity with perlin noise
        if chance(bias_field_probability):
            wavelength = torch.ceil(torch.tensor(np.random.uniform(32, 128)) / voxsize)
            strength = np.random.uniform(0, bias_field_max_strength)
            cimg *= random_bias_field(shape=shape, wavelength=wavelength,
                                      strength=strength, device=device)

        # some small corruptions that fill random slices with random intensities
        if chance(line_corruption_probability):
            for i in range(np.random.randint(1, 4)):
                indices = [slice(0, s) for s in shape]
                axis = np.random.randint(ndim)
                indices[axis] = np.random.randint(shape[axis])
                cimg[indices] = np.random.rand()

        # add gaussian noise across the entire image
        if chance(added_noise_probability):
            std = np.random.uniform(0, added_noise_max_sigma)
            cimg += torch.normal(mean=0, std=std, size=shape, device=device)

        # generate linear or circular wave (grating) artifacts across the image
        if chance(wave_artifact_probability):
            meshgrid = grid_coordinates(shape, device=device)
            if chance(0.5):
                wavelength = np.random.uniform(2, 8)
                grating = random_linear_wave(meshgrid, wavelength)
                cimg += grating * np.random.rand() * wave_artifact_max_strength
            else:
                wavelength = np.random.uniform(1, 2)
                grating = random_spherical_wave(meshgrid, wavelength)
                cimg += grating * np.random.rand() * wave_artifact_max_strength

        # ---- resizing ----

        # here we account for low-resolution images that have been upsampled to the target resolution
        if chance(resized_probability):
            # there's no need to downsample if the target resolution is less
            # than the max ds voxsize allowed
            if torch.any(voxsize < resized_max_voxsize):
                # half the time only downsample one random axis to mimic thick slice acquisitions
                if chance(resized_one_axis_probability):
                    vsa = np.full(ndim, voxsize, dtype=np.float32)
                    vsa[np.random.randint(ndim)] = np.random.uniform(voxsize.min().cpu(), resized_max_voxsize)
                    scale = tuple(1 / vsa)
                else:
                    scale = tuple(1 / np.random.uniform(voxsize, resized_max_voxsize))
                # downsample then resample, always use nearest here because if we don't enable align_corners,
                # then the image will be moved around a lot
                linear = 'trilinear' if ndim == 3 else 'bilinear'
                ds = torch.nn.functional.interpolate(cimg.unsqueeze(0), scale_factor=scale, mode=linear, align_corners=True)
                cimg = torch.nn.functional.interpolate(ds, shape, mode=linear, align_corners=True).squeeze(0)

        # ---- gamma exponentiation ----

        # one final min/max normalization across channels
        cimg -= cimg.min()
        cimg /= cimg.max()

        # gamma exponentiation of the intensities to shift signal
        # distribution in a random direction
        if chance(gamma_scaling_probability):
            gamma = np.random.uniform(-gamma_scaling_max, gamma_scaling_max)
            cimg = cimg.pow(np.exp(gamma))

        # lastly, copy the single channel into the image
        image[channel] = cimg

    return image


def random_bias_field(
    shape : List[int],
    wavelength : int,
    strength : float = 0.1,
    device: torch.device = None) -> Tensor:
    """
    Generate a random bias field with perlin noise. The bias field
    is generated by exponentiating the noise.

    Parameters
    ----------
    shape : List[int]
        Shape of the bias field.
    wavelength : int
        Wavelength of the bias field in voxels.
    strength : float, optional
        Magnitude of the field.
    device : torch.device, optional
        The device to create the field on.

    Returns
    -------
    Tensor
        Bias field image.
    """
    bias = perlin(shape=shape, wavelength=wavelength, device=device)
    bias *= strength / bias.std()
    return bias.exp()


def random_linear_wave(meshgrid : Tensor, wavelength : float) -> Tensor:
    """
    Generate a random linear grating at an arbitrary angle.

    Parameters
    ----------
    meshgrid : Tensor
        Meshgrid of the image with expected shape `(W, H[,D], N)`,
        where N is the image dimension.
    wavelength : float
        Wavelength of the wave in voxels.

    Returns
    -------
    Tensor
        Random linear wave image.
    """
    # pick two random axes and generate an angled wave grating
    ndim = meshgrid.ndim - 1
    angle = 0 if wavelength < 4 else np.random.uniform(0, np.pi)
    if ndim == 3:
        a, b = [meshgrid[..., d] for d in np.random.permutation(3)[:2]]
    elif ndim == 2:
        a, b = meshgrid[..., 0], meshgrid[..., 1]
    grating = torch.sin(2 * np.pi * (a * np.cos(angle) + b * np.sin(angle)) / wavelength)
    return grating


def random_spherical_wave(meshgrid : Tensor, wavelength : float) -> Tensor:
    """
    Generate a random spherical wave grating, with origin at any random point in the image.

    Parameters
    ----------
    meshgrid : Tensor
        Meshgrid of the image with expected shape `(W, H[,D], N)`,
        where N is the image dimension.
    wavelength : float
        Wavelength of the wave in voxels.

    Returns
    -------
    Tensor
        Random spherical wave image.
    """

    # generate a circular wave signal emanating from a random point in the image
    ndim = meshgrid.ndim - 1
    delta = [np.random.uniform(0, s) for s in meshgrid.shape[:-1]]
    if ndim == 3:
        x, y, z = [meshgrid[..., d] - delta[d]  for d in range(ndim)]
        grating = torch.sin(torch.sqrt(x ** 2 + y ** 2 + z ** 2) * wavelength)
    elif ndim == 2:
        # TODO: implement this for 2D, shouldn't be that hard, just lazy
        raise NotImplementedError('spherical waves not yet implemented for 2D images')
    return grating


def random_cropping_mask(mask: Tensor) -> Tensor:
    """
    Generate a random spatial cropping mask.

    Parameters
    ----------
    mask : Tensor
        Boolean mask image. The output mask will crop the region
        represented by this mask along any axis. The resulting mask
        will not crop the input mask by any more than 1/3 of the mask
        width on either side.

    Returns
    -------
    Tensor
        Cropping mask image.
    """
    # this code isn't pretty but basically it computes a bounding box around the
    # pertinent tissue (determined by the mask or background label), randomly selects
    # an axis (or two) as a 'crop axis', and moves that axis towards the tissue by
    # some reasonable amount
    shape = mask.shape[1:]
    ndim = len(shape)
    crop_mask = torch.zeros(shape, dtype=torch.bool, device=device)
    nonzeros = mask.nonzero()[:, 1:]
    mincoord = nonzeros.min(0)[0].cpu()
    maxcoord = nonzeros.max(0)[0].cpu() + 1
    bbox = tuple([slice(a, b) for a, b in zip(mincoord, maxcoord)])
    for _ in range(np.random.randint(1, 3)):
        axis = np.random.randint(ndim)
        s = bbox[axis]
        # don't displace the crop axis by more than 1/3 the tissue width
        displacement = int(np.random.uniform(0, (s.stop - s.start) / 3))
        cropping = [slice(0, d) for d in shape]
        # either move the low axis up and the high axis down... if that makes any sense
        if chance(0.5):
            cropping[axis] = slice(0, s.start + displacement)
        else:
            cropping[axis] = slice(s.stop - displacement, shape[axis])
        crop_mask[tuple(cropping)] = 1
    return crop_mask
