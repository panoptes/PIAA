import numpy as np

from astropy.time import Time
from astropy.wcs import WCS
from astropy.stats import sigma_clipped_stats, sigma_clip

from panoptes.utils.images import fits as fits_utils
from panoptes.utils.logger import get_root_logger
from panoptes.utils.bayer import get_rgb_data

import logging
logger = get_root_logger()
logger.setLevel(logging.DEBUG)


def moving_average(data_set, periods=3):
    """Moving average.

    Args:
        data_set (`numpy.array`): An array of values over which to perform the moving average.
        periods (int, optional): Number of periods.

    Returns:
        `numpy.array`: An array of the computed averages.
    """
    weights = np.ones(periods) / periods
    return np.convolve(data_set, weights, mode='same')


def get_pixel_drift(coords, files):
    """Get the pixel drift for a given set of coordinates.

    Args:
        coords (`astropy.coordinates.SkyCoord`): Coordinates of source.
        files (list): A list of FITS files with valid WCS.

    Returns:
        `numpy.array, numpy.array`: A 2xN array of pixel deltas where
            N=len(files)
    """
    # Get target positions for each frame
    logger.info("Getting pixel drift for {}".format(coords))
    target_pos = list()
    for fn in files:
        h0 = fits_utils.getheader(fn)
        pos = WCS(h0).all_world2pix(coords.ra, coords.dec, 1)
        target_pos.append(pos)

    target_pos = np.array(target_pos)

    # Subtract out the mean to get just the pixel deltas
    x_pos = target_pos[:, 0]
    y_pos = target_pos[:, 1]

    x_pos -= x_pos.mean()
    y_pos -= y_pos.mean()

    return x_pos, y_pos


def get_planet_phase(period, midpoint, obs_time):
    """Get planet phase from period and midpoint.

    Args:
        period (float): The length of the period in days.
        midpoint (`datetime.datetime`): The midpoint of the transit.
        obs_time (`datetime.datetime`): The time at which to compute the phase.

    Returns:
        float: The phase of the planet.
    """
    return ((Time(obs_time).mjd - Time(midpoint).mjd) % period) / period


def scintillation_index(exptime,
                        airmass,
                        elevation,
                        diameter=0.061,
                        scale_height=8000,
                        correction_coeff=1.5):
    """Calculate the scintillation index.

    A modification to Young's approximation for estimating the scintillation index, this
    uses a default correction coefficient of 1.5 (see reference).

    Note:
        The scintillation index defines the amount of scintillation and is expressed as a variance.
        Scintillation noise is the square root of the index value.

    Empirical Coefficients:
        Observatory Cmedian C Q1  CQ3
        Armazones      1.61 1.30 2.00
        La Palma       1.30 1.02 1.62
        Mauna Kea      1.63 1.34 2.02
        Paranal        1.56 1.27 1.90
        San Pedro      1.67 1.32 2.14
        Tololo         1.42 1.17 1.74

    For PANOPTES, the default lens is an 85 mm f/1.4 lens. This gives an effective
    diameter of:
        # 85 mm at f/1.4
        diameter = 85 / 1.4
        diameter = 0.061 m

    Reference:
        Osborn, J., Föhring, D., Dhillon, V. S., & Wilson, R. W. (2015).
        Atmospheric scintillation in astronomical photometry.
        Monthly Notices of the Royal Astronomical Society, 452(2), 1707–1716.
        https://doi.org/10.1093/mnras/stv1400

    """
    zenith_distance = (np.arccos(1 / airmass))

    # TODO(wtgee) make this less ugly
    return 10e-6 * (correction_coeff**2) * \
        (diameter**(-4 / 3)) * \
        (1 / exptime) * \
        (np.cos(zenith_distance)**-3) * \
        np.exp(-2 * elevation / scale_height)


def get_photon_flux_params(filter_name='V'):
    """

    Note:
        Atmospheric extinction comes from:
        http://slittlefair.staff.shef.ac.uk/teaching/phy217/lectures/principles/L04/index.html
    """
    photon_flux_values = {
        "B": {
            "lambda_c": 0.44,   # Micron
            "dlambda_ratio": 0.22,
            "flux0": 4260,      # Jansky
            "photon0": 1496,    # photons / s^-1 / cm^-2 / AA^-1
            "ref": "Bessel (1979)",
            "extinction": 0.25,  # mag/airmass
            "filter_width": 72,  # nm
        },
        "V": {
            "lambda_c": 0.55,
            "dlambda_ratio": 0.16,
            "flux0": 3640,
            "photon0": 1000,
            "ref": "Bessel (1979)",
            "extinction": 0.15,
            "filter_width": 86,  # nm
        },
        "R": {
            "lambda_c": 0.64,
            "dlambda_ratio": 0.23,
            "flux0": 3080,
            "photon0": 717,
            "ref": "Bessel (1979)",
            "extinction": 0.09,
            "filter_width": 133,  # nm
        },
    }

    return photon_flux_values.get(filter_name)


def get_adaptive_aperture(target_stamp, return_snr=False, cutoff_value=1):
    aperture_pixels = dict()
    snr = dict()

    rgb_data = get_rgb_data(target_stamp)

    for color, i in zip('rgb', range(3)):
        color_data = rgb_data[i]

        # Get the background
        s_mean, s_med, s_std = sigma_clipped_stats(color_data.compressed())

        # Subtract background
        color_data = color_data - s_med

        # Get SNR of each pixel
        noise0 = np.sqrt(np.abs(color_data) + 10.5**2)
        snr0 = color_data / noise0

        # Weight each pixel according to SNR
        w0 = snr0**2 / (snr0).sum()
        weighted_snr = snr0 * w0

        weighted_sort_snr = np.sort(weighted_snr.flatten().filled(0))[::-1]
        weighted_sort_idx = np.argsort((weighted_snr).flatten().filled(0))[::-1]

        # Running sum of SNR
        snr_pixel_sum = np.cumsum(weighted_sort_snr)

        # Snip to first fourth
        snr_pixel_sum = snr_pixel_sum[:int(len(weighted_sort_snr) / 4)]

        # Use gradient to determine cutoff (to zero)
        snr_pixel_gradient = np.gradient(snr_pixel_sum)

        # Get gradient above cutoff value
        top_snr_gradient = snr_pixel_gradient[snr_pixel_gradient > cutoff_value]

        # Get the positions for the matching pixels
        best_pixel_idx = weighted_sort_idx[:len(top_snr_gradient)]

        # Get the original index position in the unflattened matrix
        aperture_pixels[color] = [idx
                                  for idx
                                  in zip(*np.unravel_index(best_pixel_idx,
                                                           color_data.shape))]

        snr[color] = (snr_pixel_sum, snr_pixel_gradient)

    if return_snr:
        return aperture_pixels, snr
    else:
        return aperture_pixels


def get_snr_growth_aperture(target_stamp=None,
                            target_psc=None,
                            frame_idx=None,
                            make_plots=False,
                            target_dir=None,
                            picid=None,
                            plot_title=None,
                            extra_pixels=1,
                            ):
    # Get the target stamp if full PSC passed.
    if target_stamp is None:
        if target_psc is not None and frame_idx is not None:
            stamp_size = int(np.sqrt(target_psc.shape[1]))
            # Get the stamp
            target_stamp = np.array(target_psc.iloc[frame_idx]).reshape(stamp_size, stamp_size)
        else:
            raise UserWarning(f'Must pass either target_stamp or target_psc and a frame_idx.')

    rgb_data = get_rgb_data(target_stamp)

    aperture_pixels = dict()
    for color, i in zip('rgb', range(len(rgb_data))):
        color_data = rgb_data[i]

        # Get the background
        s_mean, s_med, s_std = sigma_clipped_stats(color_data.compressed())

        color_sort = np.sort((color_data - s_med).flatten().filled(0))[::-1]
        color_sort_index = np.argsort((color_data).flatten().filled(0))[::-1]

        snr = list()
        for k, pix in enumerate(color_sort):
            signal = color_sort[:k + 1].sum()
            noise = np.sqrt(signal + ((k + 1) * 10.5)**2)
            snr.append(signal / noise)

        snr = np.array(snr)

        # Peak of growth curve
        max_idx = int(snr.argmax() + extra_pixels)

        aperture_pixels[color] = [idx
                                  for idx
                                  in zip(*np.unravel_index(
                                      color_sort_index[:max_idx],
                                      color_data.shape
                                  ))]

    return aperture_pixels


def make_sigma_masked_stamps(rgb_data, sigma_thresh=2.5, as_dict=True):
    stamps = dict()
    for i, color in enumerate('rgb'):
        color_data = rgb_data[i]

        # Mask the pixels that are *below* sigma threshold
        # This combines the color mask with sigma mask
        m0 = np.logical_or(
            sigma_clip(color_data, sigma=sigma_thresh).mask,
            color_data.mask
        )
        # Create the masked samp
        masked_stamp = np.ma.array(color_data, mask=~m0)
        stamps[color] = masked_stamp

    if not as_dict:
        stamps = list(stamps.values())

    return stamps