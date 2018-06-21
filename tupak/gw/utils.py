import logging
import os

import numpy as np
from gwpy.signal import filter_design
from gwpy.timeseries import TimeSeries
from scipy import signal

from tupak.core.utils import gps_time_to_gmst, ra_dec_to_theta_phi, speed_of_light, nfft


def asd_from_freq_series(freq_data, df):
    """
    Calculate the ASD from the frequency domain output of gaussian_noise()

    Parameters
    -------
    freq_data: array_like
        Array of complex frequency domain data
    df: float
        Spacing of freq_data, 1/(segment length) used to generate the gaussian noise

    Returns
    -------
    array_like: array of real-valued normalized frequency domain ASD data

    """
    return np.absolute(freq_data) * 2 * df**0.5


def psd_from_freq_series(freq_data, df):
    """
    Calculate the PSD from the frequency domain output of gaussian_noise()
    Calls asd_from_freq_series() and squares the output

    Parameters
    -------
    freq_data: array_like
        Array of complex frequency domain data
    df: float
        Spacing of freq_data, 1/(segment length) used to generate the gaussian noise

    Returns
    -------
    array_like: Real-valued normalized frequency domain PSD data

    """
    return np.power(asd_from_freq_series(freq_data, df), 2)


def time_delay_geocentric(detector1, detector2, ra, dec, time):
    """
    Calculate time delay between two detectors in geocentric coordinates based on XLALArrivaTimeDiff in TimeDelay.c
    Parameters
    -------
    detector1: array_like
        Cartesian coordinate vector for the first detector in the geocentric frame
        generated by the Interferometer class as self.vertex.
    detector2: array_like
        Cartesian coordinate vector for the second detector in the geocentric frame.
        To get time delay from Earth center, use detector2 = np.array([0,0,0])
    ra: float
        Right ascension of the source in radians
    dec: float
        Declination of the source in radians
    time: float
        GPS time in the geocentric frame

    Returns
    -------
    float: Time delay between the two detectors in the geocentric frame

    """
    gmst = gps_time_to_gmst(time)
    theta, phi = ra_dec_to_theta_phi(ra, dec, gmst)
    omega = np.array([np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta)])
    delta_d = detector2 - detector1
    return np.dot(omega, delta_d) / speed_of_light


def get_polarization_tensor(ra, dec, time, psi, mode):
    """
    Calculate the polarization tensor for a given sky location and time

    See Nishizawa et al. (2009) arXiv:0903.0528 for definitions of the polarisation tensors.
    [u, v, w] represent the Earth-frame
    [m, n, omega] represent the wave-frame
    Note: there is a typo in the definition of the wave-frame in Nishizawa et al.
    Parameters
    -------
    ra: float
        right ascension in radians
    dec: float
        declination in radians
    time: float
        geocentric GPS time
    psi: float
        binary polarisation angle counter-clockwise about the direction of propagation
    mode: str
        polarisation mode

    Returns
    -------
    array_like: A 3x3 representation of the polarization_tensor for the specified mode.

    """
    greenwich_mean_sidereal_time = gps_time_to_gmst(time)
    theta, phi = ra_dec_to_theta_phi(ra, dec, greenwich_mean_sidereal_time)
    u = np.array([np.cos(phi) * np.cos(theta), np.cos(theta) * np.sin(phi), -np.sin(theta)])
    v = np.array([-np.sin(phi), np.cos(phi), 0])
    m = -u * np.sin(psi) - v * np.cos(psi)
    n = -u * np.cos(psi) + v * np.sin(psi)

    if mode.lower() == 'plus':
        return np.einsum('i,j->ij', m, m) - np.einsum('i,j->ij', n, n)
    elif mode.lower() == 'cross':
        return np.einsum('i,j->ij', m, n) + np.einsum('i,j->ij', n, m)
    elif mode.lower() == 'breathing':
        return np.einsum('i,j->ij', m, m) + np.einsum('i,j->ij', n, n)

    omega = np.cross(m, n)
    if mode.lower() == 'longitudinal':
        return np.sqrt(2) * np.einsum('i,j->ij', omega, omega)
    elif mode.lower() == 'x':
        return np.einsum('i,j->ij', m, omega) + np.einsum('i,j->ij', omega, m)
    elif mode.lower() == 'y':
        return np.einsum('i,j->ij', n, omega) + np.einsum('i,j->ij', omega, n)
    else:
        logging.warning("{} not a polarization mode!".format(mode))
        return None


def get_vertex_position_geocentric(latitude, longitude, elevation):
    """
    Calculate the position of the IFO vertex in geocentric coordinates in meters.

    Based on arXiv:gr-qc/0008066 Eqs. B11-B13 except for the typo in the definition of the local radius.
    See Section 2.1 of LIGO-T980044-10 for the correct expression

    Parameters
    -------
    latitude: float
        Latitude in radians
    longitude:
        Longitude in radians
    elevation:
        Elevation in meters

    Returns
    -------
    array_like: A 3D representation of the geocentric vertex position

    """
    semi_major_axis = 6378137  # for ellipsoid model of Earth, in m
    semi_minor_axis = 6356752.314  # in m
    radius = semi_major_axis**2 * (semi_major_axis**2 * np.cos(latitude)**2
                                   + semi_minor_axis**2 * np.sin(latitude)**2)**(-0.5)
    x_comp = (radius + elevation) * np.cos(latitude) * np.cos(longitude)
    y_comp = (radius + elevation) * np.cos(latitude) * np.sin(longitude)
    z_comp = ((semi_minor_axis / semi_major_axis)**2 * radius + elevation) * np.sin(latitude)
    return np.array([x_comp, y_comp, z_comp])


def inner_product(aa, bb, frequency, PSD):
    """
    Calculate the inner product defined in the matched filter statistic

    Parameters
    -------
    aa, bb: array_like
        Single-sided Fourier transform, created, e.g., by the nfft function above
    frequency: array_like
        An array of frequencies associated with aa, bb, also returned by nfft
    PSD: tupak.gw.detector.PowerSpectralDensity

    Returns
    -------
    The matched filter inner product for aa and bb

    """
    PSD_interp = PSD.power_spectral_density_interpolated(frequency)

    # calculate the inner product
    integrand = np.conj(aa) * bb / PSD_interp

    df = frequency[1] - frequency[0]
    integral = np.sum(integrand) * df
    return 4. * np.real(integral)


def noise_weighted_inner_product(aa, bb, power_spectral_density, time_duration):
    """
    Calculate the noise weighted inner product between two arrays.

    Parameters
    ----------
    aa: array_like
        Array to be complex conjugated
    bb: array_like
        Array not to be complex conjugated
    power_spectral_density: array_like
        Power spectral density of the noise
    time_duration: float
        time_duration of the data

    Returns
    ------
    Noise-weighted inner product.
    """

    integrand = np.conj(aa) * bb / power_spectral_density
    return 4 / time_duration * np.sum(integrand)


def matched_filter_snr_squared(signal, interferometer, time_duration):
    """

    Parameters
    ----------
    signal: array_like
        Array containing the signal
    interferometer: tupak.gw.detector.Interferometer
        Interferometer which we want to have the data and noise from
    time_duration: float
        Time duration of the signal

    Returns
    -------
    float: The matched filter signal to noise ratio squared

    """
    return noise_weighted_inner_product(
        signal, interferometer.frequency_domain_strain,
        interferometer.power_spectral_density_array, time_duration)


def optimal_snr_squared(signal, interferometer, time_duration):
    """

    Parameters
    ----------
    signal: array_like
        Array containing the signal
    interferometer: tupak.gw.detector.Interferometer
        Interferometer which we want to have the data and noise from
    time_duration: float
        Time duration of the signal

    Returns
    -------
    float: The optimal signal to noise ratio possible squared
    """
    return noise_weighted_inner_product(signal, signal, interferometer.power_spectral_density_array, time_duration)


def get_event_time(event):
    """
    Get the merger time for known GW events.

    We currently know about:
        GW150914
        LVT151012
        GW151226
        GW170104
        GW170608
        GW170814
        GW170817

    Parameters
    ----------
    event: str
        Event descriptor, this can deal with some prefixes, e.g., '150914', 'GW150914', 'LVT151012'

    Returns
    ------
    event_time: float
        Merger time
    """
    event_times = {'150914': 1126259462.422, '151012': 1128678900.4443,  '151226': 1135136350.65,
                   '170104': 1167559936.5991, '170608': 1180922494.4902, '170814': 1186741861.5268,
                   '170817': 1187008882.4457}
    if 'GW' or 'LVT' in event:
        event = event[-6:]

    try:
        event_time = event_times[event[-6:]]
        return event_time
    except KeyError:
        print('Unknown event {}.'.format(event))
        return None


def get_open_strain_data(
        name, t1, t2, outdir, cache=False, raw_data_file=None, **kwargs):
    """ A function which accesses the open strain data

    This uses `gwpy` to download the open data and then saves a cached copy for
    later use

    Parameters
    ----------
    name: str
        The name of the detector to get data for
    t1, t2: float
        The GPS time of the start and end of the data
    outdir: str
        The output directory to place data in
    cache: bool
        If true, cache the data
    **kwargs:
        Passed to `gwpy.timeseries.TimeSeries.fetch_open_data`
    raw_data_file

    Returns
    -----------
    strain: gwpy.timeseries.TimeSeries

    """
    filename = '{}/{}_{}_{}.txt'.format(outdir, name, t1, t2)
    if raw_data_file is not None:
        logging.info('Using raw_data_file {}'.format(raw_data_file))
        strain = TimeSeries.read(raw_data_file)
        if (t1 > strain.times[0].value) and (t2 < strain.times[-1].value):
            logging.info('Using supplied raw data file')
            strain = strain.crop(t1, t2)
        else:
            raise ValueError('Supplied file does not contain requested data')
    elif os.path.isfile(filename) and cache:
        logging.info('Using cached data from {}'.format(filename))
        strain = TimeSeries.read(filename)
    else:
        logging.info('Fetching open data ...')
        strain = TimeSeries.fetch_open_data(name, t1, t2, **kwargs)
        logging.info('Saving data to {}'.format(filename))
        strain.write(filename)
    return strain


def read_frame_file(file_name, t1, t2, channel=None, buffer_time=1, **kwargs):
    """ A function which accesses the open strain data

    This uses `gwpy` to download the open data and then saves a cached copy for
    later use

    Parameters
    ----------
    file_name: str
        The name of the frame to read
    t1, t2: float
        The GPS time of the start and end of the data
    buffer_time: float
        Read in data with `t1-buffer_time` and `t2+buffer_time`
    channel: str
        The name of the channel being searched for, some standard channel names are attempted
        if channel is not specified or if specified channel is not found.
    **kwargs:
        Passed to `gwpy.timeseries.TimeSeries.fetch_open_data`

    Returns
    -----------
    strain: gwpy.timeseries.TimeSeries

    """
    loaded = False
    strain = None
    if channel is not None:
        try:
            strain = TimeSeries.read(source=file_name, channel=channel, start=t1, end=t2, **kwargs)
            loaded = True
            logging.info('Successfully loaded {}.'.format(channel))
        except RuntimeError:
            logging.warning('Channel {} not found. Trying preset channel names'.format(channel))
    for channel_type in ['GDS-CALIB_STRAIN', 'DCS-CALIB_STRAIN_C01', 'DCS-CALIB_STRAIN_C02']:
        for ifo_name in ['H1', 'L1']:
            channel = '{}:{}'.format(ifo_name, channel_type)
            if loaded:
                continue
            try:
                strain = TimeSeries.read(source=file_name, channel=channel, start=t1-buffer_time, end=t2+buffer_time, **kwargs)
                loaded = True
                logging.info('Successfully loaded {}.'.format(channel))
            except RuntimeError:
                pass

    if loaded:
        return strain
    else:
        logging.warning('No data loaded.')
        return None


def process_strain_data(strain, alpha=0.25, filter_freq=1024):
    """
    Helper function to obtain an Interferometer instance with appropriate
    PSD and data, given an center_time.

    Parameters
    ----------
    strain: array_like
        Strain data to be processed
    alpha: float
        The tukey window shape parameter passed to `scipy.signal.tukey`.
    filter_freq: float
        Low pass filter frequency

    Returns
    -------
    tupak.detector.Interferometer: An Interferometer instance with a PSD and frequency-domain strain data.

    """

    sampling_frequency = int(strain.sample_rate.value)

    # Low pass filter
    bp = filter_design.lowpass(filter_freq, strain.sample_rate)
    strain = strain.filter(bp, filtfilt=True)
    strain = strain.crop(*strain.span.contract(1))

    time_series = strain.times.value

    # Apply Tukey window
    strain = strain * signal.windows.tukey(len(time_series), alpha=alpha)
    frequency_domain_strain, frequencies = nfft(strain.value, sampling_frequency)
    return frequency_domain_strain, frequencies
