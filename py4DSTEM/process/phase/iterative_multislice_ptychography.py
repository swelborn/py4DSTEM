"""
Module for reconstructing phase objects from 4DSTEM datasets using iterative methods,
namely multislice ptychography.
"""

import warnings
from typing import Mapping, Sequence, Tuple, Union

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.gridspec import GridSpec
from mpl_toolkits.axes_grid1 import ImageGrid, make_axes_locatable

try:
    import cupy as cp
except ImportError:
    cp = None

from py4DSTEM.io import DataCube
from py4DSTEM.process.phase.iterative_base_class import PhaseReconstruction
from py4DSTEM.process.phase.utils import (
    ComplexProbe,
    fft_shift,
    generate_batches,
    polar_aliases,
    polar_symbols,
    spatial_frequencies,
)
from py4DSTEM.process.utils import electron_wavelength_angstrom, get_CoM, get_shifted_ar
from py4DSTEM.utils.tqdmnd import tqdmnd

warnings.simplefilter(action="always", category=UserWarning)


class MultislicePtychographicReconstruction(PhaseReconstruction):
    """
    Multislice Ptychographic Reconstruction Class.

    Diffraction intensities dimensions  : (Rx,Ry,Qx,Qy)
    Reconstructed probe dimensions      : (Sx,Sy)
    Reconstructed object dimensions     : (T,Px,Py)

    such that (Sx,Sy) is the region-of-interest (ROI) size of our probe
    and (Px,Py) is the padded-object size we position our ROI around in
    each of the T slices.

    Parameters
    ----------
    datacube: DataCube
        Input 4D diffraction pattern intensities
    energy: float
        The electron energy of the wave functions in eV
    num_slices: int
        Number of slices to use in the forward model
    slice_thicknesses: float or Sequence[float]
        Slice thicknesses. If float, all slices are assigned the same thickness
    semiangle_cutoff: float, optional
        Semiangle cutoff for the initial probe guess
    rolloff: float, optional
        Semiangle rolloff for the initial probe guess
    vacuum_probe_intensity: np.ndarray, optional
        Vacuum probe to use as intensity aperture for initial probe guess
    polar_parameters: dict, optional
        Mapping from aberration symbols to their corresponding values. All aberration
        magnitudes should be given in Å and angles should be given in radians.
    diffraction_intensities_shape: Tuple[int,int], optional
        Pixel dimensions (Qx',Qy') of the resampled diffraction intensities
        If None, no resampling of diffraction intenstities is performed
    reshaping_method: str, optional
        Method to use for reshaping, either 'bin, 'bilinear', or 'fourier' (default)
    probe_roi_shape, (int,int), optional
            Padded diffraction intensities shape.
            If None, no padding is performed
    object_padding_px: Tuple[int,int], optional
        Pixel dimensions to pad object with
        If None, the padding is set to half the probe ROI dimensions
    dp_mask: ndarray, optional
        Mask for datacube intensities (Qx,Qy)
    initial_object_guess: np.ndarray, optional
        Initial guess for complex-valued object of dimensions (Px,Py)
        If None, initialized to 1.0j
    initial_probe_guess: np.ndarray, optional
        Initial guess for complex-valued probe of dimensions (Sx,Sy). If None,
        initialized to ComplexProbe with semiangle_cutoff, energy, and aberrations
    initial_scan_positions: np.ndarray, optional
        Probe positions in Å for each diffraction intensity
        If None, initialized to a grid scan
    verbose: bool, optional
        If True, class methods will inherit this and print additional information
    device: str, optional
        Calculation device will be perfomed on. Must be 'cpu' or 'gpu'
    kwargs:
        Provide the aberration coefficients as keyword arguments.
    """

    def __init__(
        self,
        datacube: DataCube,
        energy: float,
        num_slices: int,
        slice_thicknesses: Union[float, Sequence[float]],
        semiangle_cutoff: float = None,
        rolloff: float = 2.0,
        vacuum_probe_intensity: np.ndarray = None,
        polar_parameters: Mapping[str, float] = None,
        diffraction_intensities_shape: Tuple[int, int] = None,
        reshaping_method: str = "fourier",
        probe_roi_shape: Tuple[int, int] = None,
        object_padding_px: Tuple[int, int] = None,
        dp_mask: np.ndarray = None,
        initial_object_guess: np.ndarray = None,
        initial_probe_guess: np.ndarray = None,
        initial_scan_positions: np.ndarray = None,
        verbose: bool = True,
        device: str = "cpu",
        **kwargs,
    ):
        if device == "cpu":
            self._xp = np
            self._asnumpy = np.asarray
            from scipy.ndimage import gaussian_filter

            self._gaussian_filter = gaussian_filter
        elif device == "gpu":
            self._xp = cp
            self._asnumpy = cp.asnumpy
            from cupyx.scipy.ndimage import gaussian_filter

            self._gaussian_filter = gaussian_filter
        else:
            raise ValueError(f"device must be either 'cpu' or 'gpu', not {device}")

        for key in kwargs.keys():
            if (key not in polar_symbols) and (key not in polar_aliases.keys()):
                raise ValueError("{} not a recognized parameter".format(key))

        self._polar_parameters = dict(zip(polar_symbols, [0.0] * len(polar_symbols)))

        if polar_parameters is None:
            polar_parameters = {}

        polar_parameters.update(kwargs)
        self._set_polar_parameters(polar_parameters)

        slice_thicknesses = np.array(slice_thicknesses)
        if slice_thicknesses.shape == ():
            slice_thicknesses = np.tile(slice_thicknesses, num_slices - 1)
        elif slice_thicknesses.shape[0] != (num_slices - 1):
            raise ValueError(
                (
                    f"slice_thicknesses must have length {num_slices - 1}, "
                    f"not {slice_thicknesses.shape[0]}."
                )
            )

        self._energy = energy
        self._num_slices = num_slices
        self._slice_thicknesses = slice_thicknesses
        self._semiangle_cutoff = semiangle_cutoff
        self._rolloff = rolloff
        self._vacuum_probe_intensity = vacuum_probe_intensity
        self._diffraction_intensities_shape = diffraction_intensities_shape
        self._reshaping_method = reshaping_method
        self._probe_roi_shape = probe_roi_shape
        self._object = initial_object_guess
        self._probe = initial_probe_guess
        self._scan_positions = initial_scan_positions
        self._datacube = datacube
        self._dp_mask = dp_mask
        self._verbose = verbose
        self._object_padding_px = object_padding_px
        self._preprocessed = False

    def _precompute_propagator_arrays(
        self,
        gpts: Tuple[int, int],
        sampling: Tuple[float, float],
        energy: float,
        slice_thicknesses: Sequence[float],
    ):
        """
        Precomputes propagator arrays complex wave-function will be convolved by,
        for all slice thicknesses.

        Parameters
        ----------
        gpts: Tuple[int,int]
            Wavefunction pixel dimensions
        sampling: Tuple[float,float]
            Wavefunction sampling in A
        energy: float
            The electron energy of the wave functions in eV
        slice_thicknesses: Sequence[float]
            Array of slice thicknesses in A

        Returns
        -------
        propagator_arrays: np.ndarray
            (T,Sx,Sy) shape array storing propagator arrays
        """
        xp = self._xp

        # Frequencies
        kx, ky = spatial_frequencies(gpts, sampling)
        kx = xp.asarray(kx)
        ky = xp.asarray(ky)

        # Antialias masks
        k = xp.sqrt(kx[:, None] ** 2 + ky[None] ** 2)
        kcut = 1 / max(sampling) / 2 * 2 / 3.0  # 2/3 cutoff
        antialias_mask = 0.5 * (
            1 + xp.cos(np.pi * (k - kcut + 0.1) / 0.1)
        )  # 0.1 rolloff
        antialias_mask[k > kcut] = 0.0
        antialias_mask = xp.where(k > kcut - 0.1, antialias_mask, xp.ones_like(k))

        # Propagators
        wavelength = electron_wavelength_angstrom(energy)
        num_slices = slice_thicknesses.shape[0]
        propagators = xp.empty((num_slices,) + k.shape, dtype=xp.complex64)
        for i, dz in enumerate(slice_thicknesses):
            propagators[i] = xp.exp(
                1.0j * (-(kx**2)[:, None] * np.pi * wavelength * dz)
            )
            propagators[i] *= xp.exp(
                1.0j * (-(ky**2)[None] * np.pi * wavelength * dz)
            )

        return propagators * antialias_mask

    def _propagate_array(self, array: np.ndarray, propagator_array: np.ndarray):
        """
        Propagates array by Fourier convolving array with propagator_array.

        Parameters
        ----------
        array: np.ndarray
            Wavefunction array to be convolved
        propagator_array: np.ndarray
            Propagator array to convolve array with

        Returns
        -------
        propagated_array: np.ndarray
            Fourier-convolved array
        """
        xp = self._xp

        return xp.fft.ifft2(xp.fft.fft2(array) * propagator_array)

    def preprocess(
        self,
        fit_function: str = "plane",
        plot_center_of_mass: str = "default",
        plot_rotation: bool = True,
        maximize_divergence: bool = False,
        rotation_angles_deg: np.ndarray = np.arange(-89.0, 90.0, 1.0),
        plot_probe_overlaps: bool = True,
        force_com_rotation: float = None,
        force_com_transpose: float = None,
        force_com_shifts: float = None,
        **kwargs,
    ):
        """
        Ptychographic preprocessing step.
        Calls the base class methods:

        _extract_intensities_and_calibrations_from_datacube,
        _compute_center_of_mass(),
        _solve_CoM_rotation(),
        _normalize_diffraction_intensities()
        _calculate_scan_positions_in_px()

        Additionally, it initializes an (T,Px,Py) array of 1.0j
        and a complex probe using the specified polar parameters.

        Parameters
        ----------
        fit_function: str, optional
            2D fitting function for CoM fitting. One of 'plane','parabola','bezier_two'
        plot_center_of_mass: str, optional
            If 'default', the corrected CoM arrays will be displayed
            If 'all', the computed and fitted CoM arrays will be displayed
        plot_rotation: bool, optional
            If True, the CoM curl minimization search result will be displayed
        maximize_divergence: bool, optional
            If True, the divergence of the CoM gradient vector field is maximized
        rotation_angles_deg: np.darray, optional
            Array of angles in degrees to perform curl minimization over
        plot_probe_overlaps: bool, optional
            If True, initial probe overlaps scanned over the object will be displayed
        force_com_rotation: float (degrees), optional
            Force relative rotation angle between real and reciprocal space
        force_com_transpose: bool, optional
            Force whether diffraction intensities need to be transposed.
        force_com_shifts: tuple of ndarrays (CoMx, CoMy)
            Amplitudes come from diffraction patterns shifted with
            the CoM in the upper left corner for each probe unless
            shift is overwritten.

        Returns
        --------
        self: MultislicePtychographicReconstruction
            Self to accommodate chaining
        """
        xp = self._xp
        asnumpy = self._asnumpy

        (
            self._datacube,
            self._vacuum_probe_intensity,
            self._dp_mask,
            force_com_shifts,
        ) = self._preprocess_datacube_and_vacuum_probe(
            self._datacube,
            diffraction_intensities_shape=self._diffraction_intensities_shape,
            reshaping_method=self._reshaping_method,
            probe_roi_shape=self._probe_roi_shape,
            vacuum_probe_intensity=self._vacuum_probe_intensity,
            dp_mask=self._dp_mask,
            com_shifts=force_com_shifts,
        )

        self._intensities = self._extract_intensities_and_calibrations_from_datacube(
            self._datacube,
            require_calibrations=True,
        )

        (
            self._com_measured_x,
            self._com_measured_y,
            self._com_fitted_x,
            self._com_fitted_y,
            self._com_normalized_x,
            self._com_normalized_y,
        ) = self._calculate_intensities_center_of_mass(
            self._intensities,
            dp_mask=self._dp_mask,
            fit_function=fit_function,
            com_shifts=force_com_shifts,
        )

        (
            self._rotation_best_rad,
            self._rotation_best_transpose,
            self._com_x,
            self._com_y,
            self.com_x,
            self.com_y,
        ) = self._solve_for_center_of_mass_relative_rotation(
            self._com_measured_x,
            self._com_measured_y,
            self._com_normalized_x,
            self._com_normalized_y,
            rotation_angles_deg=rotation_angles_deg,
            plot_rotation=plot_rotation,
            plot_center_of_mass=plot_center_of_mass,
            maximize_divergence=maximize_divergence,
            force_com_rotation=force_com_rotation,
            force_com_transpose=force_com_transpose,
            **kwargs,
        )

        (
            self._amplitudes,
            self._mean_diffraction_intensity,
        ) = self._normalize_diffraction_intensities(
            self._intensities,
            self._com_fitted_x,
            self._com_fitted_y,
        )

        # explicitly delete namespace
        self._num_diffraction_patterns = self._amplitudes.shape[0]
        self._region_of_interest_shape = np.array(self._amplitudes.shape[-2:])
        del self._intensities

        self._positions_px = self._calculate_scan_positions_in_pixels(
            self._scan_positions
        )

        # Object Initialization
        if self._object is None:
            pad_x, pad_y = self._object_padding_px
            p, q = np.max(self._positions_px, axis=0)
            p = np.max([np.round(p + pad_x), self._region_of_interest_shape[0]]).astype(
                int
            )
            q = np.max([np.round(q + pad_y), self._region_of_interest_shape[1]]).astype(
                int
            )
            self._object = xp.ones((self._num_slices, p, q), dtype=xp.complex64)
        else:
            self._object = xp.asarray(self._object, dtype=xp.complex64)

        self._object_initial = self._object.copy()
        self._object_shape = self._object.shape[-2:]

        self._positions_px = xp.asarray(self._positions_px, dtype=xp.float32)
        self._positions_px_com = xp.mean(self._positions_px, axis=0)
        self._positions_px -= self._positions_px_com - xp.array(self._object_shape) / 2
        self._positions_px_com = xp.mean(self._positions_px, axis=0)
        self._positions_px_fractional = self._positions_px - xp.round(
            self._positions_px
        )

        self._positions_px_initial = self._positions_px.copy()
        self._positions_initial = self._positions_px_initial.copy()
        self._positions_initial[:, 0] *= self.sampling[0]
        self._positions_initial[:, 1] *= self.sampling[1]

        # Vectorized Patches
        (
            self._vectorized_patch_indices_row,
            self._vectorized_patch_indices_col,
        ) = self._extract_vectorized_patch_indices()

        # Probe Initialization
        if self._probe is None:
            if self._vacuum_probe_intensity is not None:
                self._semiangle_cutoff = np.inf
                self._vacuum_probe_intensity = xp.asarray(self._vacuum_probe_intensity)
                probe_x0, probe_y0 = get_CoM(
                    self._vacuum_probe_intensity, device="cpu" if xp is np else "gpu"
                )
                shift_x = self._region_of_interest_shape[0] // 2 - probe_x0
                shift_y = self._region_of_interest_shape[1] // 2 - probe_y0
                self._vacuum_probe_intensity = get_shifted_ar(
                    self._vacuum_probe_intensity,
                    shift_x,
                    shift_y,
                    bilinear=True,
                    device="cpu" if xp is np else "gpu",
                )

            self._probe = (
                ComplexProbe(
                    gpts=self._region_of_interest_shape,
                    sampling=self.sampling,
                    energy=self._energy,
                    semiangle_cutoff=self._semiangle_cutoff,
                    rolloff=self._rolloff,
                    vacuum_probe_intensity=self._vacuum_probe_intensity,
                    parameters=self._polar_parameters,
                    device="cpu" if xp is np else "gpu",
                )
                .build()
                ._array
            )

        else:
            if isinstance(self._probe, ComplexProbe):
                if self._probe._gpts != self._region_of_interest_shape:
                    raise ValueError()
                if hasattr(self._probe, "_array"):
                    self._probe = self._probe._array
                else:
                    self._probe._xp = xp
                    self._probe = self._probe.build()._array
            else:
                self._probe = xp.asarray(self._probe, dtype=xp.complex64)

        # Normalize probe to match mean diffraction intensity
        probe_intensity = xp.sum(xp.abs(xp.fft.fft2(self._probe)) ** 2)
        self._probe *= np.sqrt(self._mean_diffraction_intensity / probe_intensity)

        self._probe_initial = self._probe.copy()
        self._probe_initial_fft_amplitude = xp.abs(xp.fft.fft2(self._probe_initial))

        # Precomputed propagator arrays
        self._propagator_arrays = self._precompute_propagator_arrays(
            self._region_of_interest_shape,
            self.sampling,
            self._energy,
            self._slice_thicknesses,
        )

        if plot_probe_overlaps:
            shifted_probes = fft_shift(self._probe, self._positions_px_fractional, xp)
            probe_intensities = xp.abs(shifted_probes) ** 2
            probe_overlap = self._sum_overlapping_patches_bincounts(probe_intensities)

            figsize = kwargs.get("figsize", (8, 4))
            cmap = kwargs.get("cmap", "Greys_r")
            kwargs.pop("figsize", None)
            kwargs.pop("cmap", None)

            extent = [
                0,
                self.sampling[1] * self._object_shape[1],
                self.sampling[0] * self._object_shape[0],
                0,
            ]

            probe_extent = [
                0,
                self.sampling[1] * self._region_of_interest_shape[1],
                self.sampling[0] * self._region_of_interest_shape[0],
                0,
            ]

            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=figsize)

            ax1.imshow(
                asnumpy(xp.abs(self._probe) ** 2),
                extent=probe_extent,
                cmap=cmap,
                **kwargs,
            )
            ax1.set_ylabel("x [A]")
            ax1.set_xlabel("y [A]")
            ax1.set_title("Initial Probe Intensity")

            ax2.imshow(
                asnumpy(probe_overlap),
                extent=extent,
                cmap=cmap,
                **kwargs,
            )
            ax2.scatter(
                self.positions[:, 1],
                self.positions[:, 0],
                s=2.5,
                color=(1, 0, 0, 1),
            )
            ax2.set_ylabel("x [A]")
            ax2.set_xlabel("y [A]")
            ax2.set_xlim((extent[0], extent[1]))
            ax2.set_ylim((extent[2], extent[3]))
            ax2.set_title("Object Field of View")

            fig.tight_layout()

        self._preprocessed = True

        return self

    def _overlap_projection(self, current_object, current_probe):
        """
        Ptychographic overlap projection method.

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate

        Returns
        --------
        propagated_probes: np.ndarray
            Final shifted probes following N-1 propagations
        object_patches: np.ndarray
            Patched object view
        transmitted_probes: np.ndarray
            Transmitted probes at each layer
        """

        xp = self._xp

        propagated_probes = fft_shift(current_probe, self._positions_px_fractional, xp)

        object_patches = current_object[
            :, self._vectorized_patch_indices_row, self._vectorized_patch_indices_col
        ]

        transmitted_probes = xp.empty_like(object_patches)

        for s in range(self._num_slices):
            # transmit
            transmitted_probes[s] = object_patches[s] * propagated_probes

            # propagate
            if s + 1 < self._num_slices:
                propagated_probes = self._propagate_array(
                    transmitted_probes[s], self._propagator_arrays[s]
                )

        return propagated_probes, object_patches, transmitted_probes

    def _gradient_descent_fourier_projection(
        self, amplitudes, final_transmitted_probes
    ):
        """
        Ptychographic fourier projection method for GD method.

        Parameters
        --------
        amplitudes: np.ndarray
            Normalized measured amplitudes
        final_transmitted_probes: np.ndarray
            Transmitted probes at last layer

        Returns
        --------
        exit_waves:np.ndarray
            Exit wave difference
        error: float
            Reconstruction error
        """

        xp = self._xp
        fourier_exit_waves = xp.fft.fft2(final_transmitted_probes)

        error = (
            xp.mean(xp.abs(amplitudes - xp.abs(fourier_exit_waves)) ** 2)
            / self._mean_diffraction_intensity
        )

        modified_exit_wave = xp.fft.ifft2(
            amplitudes * xp.exp(1j * xp.angle(fourier_exit_waves))
        )

        exit_waves = modified_exit_wave - final_transmitted_probes

        return exit_waves, error

    def _projection_sets_fourier_projection(
        self,
        amplitudes,
        final_transmitted_probes,
        exit_waves,
        projection_a,
        projection_b,
        projection_c,
    ):
        """
        Ptychographic fourier projection method for DM_AP and RAAR methods.
        Generalized projection using three parameters: a,b,c

            DM_AP(\alpha)   :   a =  -\alpha, b = 1, c = 1 + \alpha
              DM: DM_AP(1.0), AP: DM_AP(0.0)

            RAAR(\beta)     :   a = 1-2\beta, b = \beta, c = 2
              DM : RAAR(1.0)

            RRR(\gamma)     :   a = -\gamma, b = \gamma, c = 2
              DM: RRR(1.0)

            SUPERFLIP       :   a = 0, b = 1, c = 2

        Parameters
        --------
        amplitudes: np.ndarray
            Normalized measured amplitudes
        final_transmitted_probes: np.ndarray
            Transmitted probes at last layer
        exit_waves: np.ndarray
            previously estimated exit waves
        projection_a: float
        projection_b: float
        projection_c: float

        Returns
        --------
        exit_waves:np.ndarray
            Updated exit wave difference
        error: float
            Reconstruction error
        """

        xp = self._xp
        projection_x = 1 - projection_a - projection_b
        projection_y = 1 - projection_c

        if exit_waves is None:
            exit_waves = final_transmitted_probes.copy()

        fourier_exit_waves = xp.fft.fft2(final_transmitted_probes)
        error = (
            xp.mean(xp.abs(amplitudes - xp.abs(fourier_exit_waves)) ** 2)
            / self._mean_diffraction_intensity
        )

        factor_to_be_projected = (
            projection_c * final_transmitted_probes + projection_y * exit_waves
        )
        fourier_projected_factor = xp.fft.fft2(factor_to_be_projected)

        fourier_projected_factor = amplitudes * xp.exp(
            1j * xp.angle(fourier_projected_factor)
        )
        projected_factor = xp.fft.ifft2(fourier_projected_factor)

        exit_waves = (
            projection_x * exit_waves
            + projection_a * final_transmitted_probes
            + projection_b * projected_factor
        )

        return exit_waves, error

    def _forward(
        self,
        current_object,
        current_probe,
        amplitudes,
        exit_waves,
        use_projection_scheme,
        projection_a,
        projection_b,
        projection_c,
    ):
        """
        Ptychographic forward operator.
        Calls _overlap_projection() and the appropriate _fourier_projection().

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate
        amplitudes: np.ndarray
            Normalized measured amplitudes
        exit_waves: np.ndarray
            previously estimated exit waves
        use_projection_scheme: bool,
            If True, use generalized projection update
        projection_a: float
        projection_b: float
        projection_c: float

        Returns
        --------
        propagated_probes:np.ndarray
            Prop[object^n*probe^n]
        object_patches: np.ndarray
            Patched object view
        transmitted_probes: np.ndarray
            Transmitted probes at each layer
        exit_waves:np.ndarray
            Updated exit_waves
        error: float
            Reconstruction error
        """

        (
            propagated_probes,
            object_patches,
            transmitted_probes,
        ) = self._overlap_projection(current_object, current_probe)

        if use_projection_scheme:
            exit_waves, error = self._projection_sets_fourier_projection(
                amplitudes,
                transmitted_probes[-1],
                exit_waves,
                projection_a,
                projection_b,
                projection_c,
            )

        else:
            exit_waves, error = self._gradient_descent_fourier_projection(
                amplitudes, transmitted_probes[-1]
            )

        return propagated_probes, object_patches, transmitted_probes, exit_waves, error

    def _gradient_descent_adjoint(
        self,
        current_object,
        current_probe,
        object_patches,
        transmitted_probes,
        exit_waves,
        step_size,
        normalization_min,
        fix_probe,
    ):
        """
        Ptychographic adjoint operator for GD method.
        Computes object and probe update steps.

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate
        object_patches: np.ndarray
            Patched object view
        transmitted_probes: np.ndarray
            Transmitted probes at each layer
        exit_waves:np.ndarray
            Updated exit_waves
        step_size: float, optional
            Update step size
        normalization_min: float, optional
            Probe normalization minimum as a fraction of the maximum overlap intensity
        fix_probe: bool, optional
            If True, probe will not be updated

        Returns
        --------
        updated_object: np.ndarray
            Updated object estimate
        updated_probe: np.ndarray
            Updated probe estimate
        """
        xp = self._xp

        for s in reversed(range(self._num_slices)):
            probe = transmitted_probes[s]
            obj = object_patches[s]

            # back-transmit
            exit_waves *= xp.conj(obj)

            # object-update
            probe_normalization = self._sum_overlapping_patches_bincounts(
                xp.abs(probe) ** 2
            )
            probe_normalization = 1 / xp.sqrt(
                1e-16
                + ((1 - normalization_min) * probe_normalization) ** 2
                + (normalization_min * xp.max(probe_normalization)) ** 2
            )

            current_object[s] += step_size * (
                self._sum_overlapping_patches_bincounts(xp.conj(probe) * exit_waves)
                * probe_normalization
            )

            if s > 0:
                # back-propagate
                exit_waves = self._propagate_array(
                    exit_waves, xp.conj(self._propagator_arrays[s - 1])
                )
            elif not fix_probe:
                # probe-update
                object_normalization = xp.sum(
                    (xp.abs(obj) ** 2),
                    axis=0,
                )
                object_normalization = 1 / xp.sqrt(
                    1e-16
                    + ((1 - normalization_min) * object_normalization) ** 2
                    + (normalization_min * xp.max(object_normalization)) ** 2
                )

                current_probe += (
                    step_size
                    * xp.sum(
                        exit_waves,
                        axis=0,
                    )
                    * object_normalization
                )

        return current_object, current_probe

    def _projection_sets_adjoint(
        self,
        current_object,
        current_probe,
        object_patches,
        transmitted_probes,
        exit_waves,
        normalization_min,
        fix_probe,
    ):
        """
        Ptychographic adjoint operator for DM_AP and RAAR methods.
        Computes object and probe update steps.

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate
        object_patches: np.ndarray
            Patched object view
        transmitted_probes: np.ndarray
            Transmitted probes at each layer
        exit_waves:np.ndarray
            Updated exit_waves
        normalization_min: float, optional
            Probe normalization minimum as a fraction of the maximum overlap intensity
        fix_probe: bool, optional
            If True, probe will not be updated

        Returns
        --------
        updated_object: np.ndarray
            Updated object estimate
        updated_probe: np.ndarray
            Updated probe estimate
        """
        xp = self._xp

        # careful not to modify exit_waves in-place for projection set methods
        exit_waves_copy = exit_waves.copy()
        for s in reversed(range(self._num_slices)):
            probe = transmitted_probes[s]
            obj = object_patches[s]

            # back-transmit
            exit_waves_copy *= xp.conj(obj)

            # object-update
            probe_normalization = self._sum_overlapping_patches_bincounts(
                xp.abs(probe) ** 2
            )
            probe_normalization = 1 / xp.sqrt(
                1e-16
                + ((1 - normalization_min) * probe_normalization) ** 2
                + (normalization_min * xp.max(probe_normalization)) ** 2
            )

            current_object[s] = (
                self._sum_overlapping_patches_bincounts(
                    xp.conj(probe) * exit_waves_copy
                )
                * probe_normalization
            )

            if s > 0:
                # back-propagate
                exit_waves_copy = self._propagate_array(
                    exit_waves_copy, xp.conj(self._propagator_arrays[s - 1])
                )

            elif not fix_probe:
                # probe-update
                object_normalization = xp.sum(
                    (xp.abs(obj) ** 2),
                    axis=0,
                )
                object_normalization = 1 / xp.sqrt(
                    1e-16
                    + ((1 - normalization_min) * object_normalization) ** 2
                    + (normalization_min * xp.max(object_normalization)) ** 2
                )

                current_probe = (
                    xp.sum(
                        exit_waves_copy,
                        axis=0,
                    )
                    * object_normalization
                )

        return current_object, current_probe

    def _adjoint(
        self,
        current_object,
        current_probe,
        object_patches,
        transmitted_probes,
        exit_waves,
        use_projection_scheme: bool,
        step_size: float,
        normalization_min: float,
        fix_probe: bool,
    ):
        """
        Ptychographic adjoint operator for GD method.
        Computes object and probe update steps.

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate
        object_patches: np.ndarray
            Patched object view
        transmitted_probes: np.ndarray
            Transmitted probes at each layer
        exit_waves:np.ndarray
            Updated exit_waves
        step_size: float, optional
            Update step size
        normalization_min: float, optional
            Probe normalization minimum as a fraction of the maximum overlap intensity
        fix_probe: bool, optional
            If True, probe will not be updated

        Returns
        --------
        updated_object: np.ndarray
            Updated object estimate
        updated_probe: np.ndarray
            Updated probe estimate
        """

        if use_projection_scheme:
            current_object, current_probe = self._projection_sets_adjoint(
                current_object,
                current_probe,
                object_patches,
                transmitted_probes,
                exit_waves,
                normalization_min,
                fix_probe,
            )
        else:
            current_object, current_probe = self._gradient_descent_adjoint(
                current_object,
                current_probe,
                object_patches,
                transmitted_probes,
                exit_waves,
                step_size,
                normalization_min,
                fix_probe,
            )

        return current_object, current_probe

    def _position_correction(
        self,
        current_object,
        current_probe,
        transmitted_probes,
        amplitudes,
        current_positions,
        positions_step_size,
    ):
        """
        Position correction using estimated intensity gradient.

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe:np.ndarray
            fractionally-shifted probes
        transmitted_probes: np.ndarray
            Transmitted probes at each layer
        amplitudes: np.ndarray
            Measured amplitudes
        current_positions: np.ndarray
            Current positions estimate
        positions_step_size: float
            Positions step size

        Returns
        --------
        updated_positions: np.ndarray
            Updated positions estimate
        """

        xp = self._xp

        # Intensity gradient
        exit_waves_fft = xp.fft.fft2(transmitted_probes[-1])
        exit_waves_fft_conj = xp.conj(exit_waves_fft)
        estimated_intensity = xp.abs(exit_waves_fft) ** 2
        measured_intensity = amplitudes**2

        flat_shape = (transmitted_probes[-1].shape[0], -1)
        difference_intensity = (measured_intensity - estimated_intensity).reshape(
            flat_shape
        )

        # Computing perturbed exit waves one at a time to save on memory

        # dx
        propagated_probes = fft_shift(current_probe, self._positions_px_fractional, xp)
        obj_rolled_patches = current_object[
            :,
            (self._vectorized_patch_indices_row + 1) % self._object_shape[0],
            self._vectorized_patch_indices_col,
        ]

        transmitted_probes_perturbed = xp.empty_like(obj_rolled_patches)

        for s in range(self._num_slices):
            # transmit
            transmitted_probes_perturbed[s] = obj_rolled_patches[s] * propagated_probes

            # propagate
            if s + 1 < self._num_slices:
                propagated_probes = self._propagate_array(
                    transmitted_probes_perturbed[s], self._propagator_arrays[s]
                )

        exit_waves_dx_fft = exit_waves_fft - xp.fft.fft2(
            transmitted_probes_perturbed[-1]
        )

        # dy
        propagated_probes = fft_shift(current_probe, self._positions_px_fractional, xp)
        obj_rolled_patches = current_object[
            :,
            self._vectorized_patch_indices_row,
            (self._vectorized_patch_indices_col + 1) % self._object_shape[1],
        ]

        transmitted_probes_perturbed = xp.empty_like(obj_rolled_patches)

        for s in range(self._num_slices):
            # transmit
            transmitted_probes_perturbed[s] = obj_rolled_patches[s] * propagated_probes

            # propagate
            if s + 1 < self._num_slices:
                propagated_probes = self._propagate_array(
                    transmitted_probes_perturbed[s], self._propagator_arrays[s]
                )

        exit_waves_dy_fft = exit_waves_fft - xp.fft.fft2(
            transmitted_probes_perturbed[-1]
        )

        partial_intensity_dx = 2 * xp.real(
            exit_waves_dx_fft * exit_waves_fft_conj
        ).reshape(flat_shape)
        partial_intensity_dy = 2 * xp.real(
            exit_waves_dy_fft * exit_waves_fft_conj
        ).reshape(flat_shape)

        coefficients_matrix = xp.dstack((partial_intensity_dx, partial_intensity_dy))

        # positions_update = xp.einsum(
        #    "idk,ik->id", xp.linalg.pinv(coefficients_matrix), difference_intensity
        # )

        coefficients_matrix_T = coefficients_matrix.conj().swapaxes(-1, -2)
        positions_update = (
            xp.linalg.inv(coefficients_matrix_T @ coefficients_matrix)
            @ coefficients_matrix_T
            @ difference_intensity[..., None]
        )

        current_positions -= positions_step_size * positions_update[..., 0]

        return current_positions

    def _object_butterworth_constraint(self, current_object, q_lowpass, q_highpass):
        """
        Butterworth filter

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        q_lowpass: float
            Cut-off frequency in A^-1 for low-pass butterworth filter
        q_highpass: float
            Cut-off frequency in A^-1 for high-pass butterworth filter

        Returns
        --------
        constrained_object: np.ndarray
            Constrained object estimate
        """
        xp = self._xp
        qx = xp.fft.fftfreq(current_object.shape[1], self.sampling[0])
        qy = xp.fft.fftfreq(current_object.shape[2], self.sampling[1])
        qya, qxa = xp.meshgrid(qy, qx)
        qra = xp.sqrt(qxa**2 + qya**2)

        env = xp.ones_like(qra)
        if q_highpass:
            env *= 1 - 1 / (1 + (qra / q_highpass) ** 4)
        if q_lowpass:
            env *= 1 / (1 + (qra / q_lowpass) ** 4)

        current_object = xp.fft.ifft2(xp.fft.fft2(current_object) * env[None])
        return current_object

    def _object_kz_regularization_constraint(
        self, current_object, kz_regularization_gamma
    ):
        """
        Arctan regularization filter

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        kz_regularization_gamma: float
            Slice regularization strength

        Returns
        --------
        constrained_object: np.ndarray
            Constrained object estimate
        """
        xp = self._xp
        qx = xp.fft.fftfreq(current_object.shape[1], self.sampling[0])
        qy = xp.fft.fftfreq(current_object.shape[2], self.sampling[1])
        qz = xp.fft.fftfreq(current_object.shape[0], self._slice_thicknesses[0])

        qza, qxa, qya = xp.meshgrid(qz, qx, qy, indexing="ij")
        qz2 = qza**2 * kz_regularization_gamma**2
        qr2 = qxa**2 + qya**2

        w = 1 - 2 / np.pi * xp.arctan2(qz2, qr2)

        current_object = xp.fft.ifftn(xp.fft.fftn(current_object) * w)

        return current_object

    def _object_identical_slices_constraint(self, current_object):
        """
        Strong regularization forcing all slices to be identical

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate

        Returns
        --------
        constrained_object: np.ndarray
            Constrained object estimate
        """
        object_mean = current_object.mean(0, keepdims=True)
        current_object[:] = object_mean

        return current_object

    def _constraints(
        self,
        current_object,
        current_probe,
        current_positions,
        pure_phase_object,
        fix_com,
        fix_probe_fourier_amplitude,
        fix_positions,
        global_affine_transformation,
        gaussian_filter,
        gaussian_filter_sigma,
        butterworth_filter,
        q_lowpass,
        q_highpass,
        kz_regularization_filter,
        kz_regularization_gamma,
        identical_slices,
    ):
        """
        Ptychographic constraints operator.
        Calls _threshold_object_constraint() and _probe_center_of_mass_constraint()

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate
        current_positions: np.ndarray
            Current positions estimate
        pure_phase_object: bool
            If True, object amplitude is set to unity
        fix_com: bool
            If True, probe CoM is fixed to the center
        fix_probe_fourier_amplitude: bool
            If True, probe fourier amplitude is set to initial probe
        fix_positions: bool
            If True, positions are not updated
        gaussian_filter: bool
            If True, applies real-space gaussian filter
        gaussian_filter_sigma: float
            Standard deviation of gaussian kernel
        butterworth_filter: bool
            If True, applies fourier-space butterworth filter
        q_lowpass: float
            Cut-off frequency in A^-1 for low-pass butterworth filter
        q_highpass: float
            Cut-off frequency in A^-1 for high-pass butterworth filter
        kz_regularization_filter: bool
            If True, applies fourier-space arctan regularization filter
        kz_regularization_gamma: float
            Slice regularization strength
        identical_slices: bool
            If True, forces all object slices to be identical

        Returns
        --------
        constrained_object: np.ndarray
            Constrained object estimate
        constrained_probe: np.ndarray
            Constrained probe estimate
        constrained_positions: np.ndarray
            Constrained positions estimate
        """

        if gaussian_filter:
            current_object = self._object_gaussian_constraint(
                current_object, gaussian_filter_sigma, pure_phase_object
            )

        if butterworth_filter:
            current_object = self._object_butterworth_constraint(
                current_object,
                q_lowpass,
                q_highpass,
            )

        if kz_regularization_filter:
            current_object = self._object_kz_regularization_constraint(
                current_object, kz_regularization_gamma
            )

        if identical_slices:
            current_object = self._object_identical_slices_constraint(current_object)

        current_object = self._object_threshold_constraint(
            current_object, pure_phase_object
        )

        if fix_probe_fourier_amplitude:
            current_probe = self._probe_fourier_amplitude_constraint(current_probe)

        current_probe = self._probe_finite_support_constraint(current_probe)

        if fix_com:
            current_probe = self._probe_center_of_mass_constraint(current_probe)

        if not fix_positions:
            current_positions = self._positions_center_of_mass_constraint(
                current_positions
            )

            if global_affine_transformation:
                current_positions = self._positions_affine_transformation_constraint(
                    self._positions_px_initial, current_positions
                )

        return current_object, current_probe, current_positions

    def reconstruct(
        self,
        max_iter: int = 64,
        reconstruction_method: str = "gradient-descent",
        reconstruction_parameter: float = 1.0,
        max_batch_size: int = None,
        seed_random: int = None,
        step_size: float = 0.9,
        normalization_min: float = 1e-3,
        positions_step_size: float = 0.9,
        pure_phase_object_iter: int = 0,
        fix_com: bool = True,
        fix_probe_iter: int = 0,
        fix_probe_fourier_amplitude_iter: int = 0,
        fix_positions_iter: int = np.inf,
        global_affine_transformation: bool = True,
        probe_support_relative_radius: float = 1.0,
        probe_support_supergaussian_degree: float = 10.0,
        gaussian_filter_sigma: float = None,
        gaussian_filter_iter: int = np.inf,
        butterworth_filter_iter: int = np.inf,
        q_lowpass: float = None,
        q_highpass: float = None,
        kz_regularization_filter_iter: int = np.inf,
        kz_regularization_gamma: float = None,
        identical_slices_iter: int = 0,
        store_iterations: bool = False,
        progress_bar: bool = True,
        reset: bool = None,
    ):
        """
        Ptychographic reconstruction main method.

        Parameters
        --------
        max_iter: int, optional
            Maximum number of iterations to run
        reconstruction_method: str, optional
            Specifies which reconstruction algorithm to use, one of:
            "generalized-projection",
            "DM_AP" (or "difference-map_alternating-projections"),
            "RAAR" (or "relaxed-averaged-alternating-reflections"),
            "RRR" (or "relax-reflect-reflect"),
            "SUPERFLIP" (or "charge-flipping"), or
            "GD" (or "gradient_descent")
        reconstruction_parameter: float, optional
            Reconstruction parameter for various reconstruction methods above.
        reconstruction_parameter: float, optional
            Tuning parameter to interpolate b/w DM-AP and DM-RAAR
        max_batch_size: int, optional
            Max number of probes to update at once
        seed_random: int, optional
            Seeds the random number generator, only applicable when max_batch_size is not None
        step_size: float, optional
            Update step size
        normalization_min: float, optional
            Probe normalization minimum as a fraction of the maximum overlap intensity
        positions_step_size: float, optional
            Positions update step size
        pure_phase_object: bool, optional
            If True, object amplitude is set to unity
        fix_com: bool, optional
            If True, fixes center of mass of probe
        fix_probe_iter: int, optional
            Number of iterations to run with a fixed probe before updating probe estimate
        fix_probe_amplitude: int, optional
            Number of iterations to run with a fixed probe amplitude
        fix_positions_iter: int, optional
            Number of iterations to run with fixed positions before updating positions estimate
        global_affine_transformation: bool, optional
            If True, positions are assumed to be a global affine transform from initial scan
        probe_support_relative_radius: float, optional
            Radius of probe supergaussian support in scaled pixel units, between (0,1]
        probe_support_supergaussian_degree: float, optional
            Degree supergaussian support is raised to, higher is sharper cutoff
        gaussian_filter_sigma: float, optional
            Standard deviation of gaussian kernel
        gaussian_filter_iter: int, optional
            Number of iterations to run using object smoothness constraint
        butterworth_filter_iter: int, optional
            Number of iterations to run using high-pass butteworth filter
        q_lowpass: float
            Cut-off frequency in A^-1 for low-pass butterworth filter
        q_highpass: float
            Cut-off frequency in A^-1 for high-pass butterworth filter
        kz_regularization_filter_iter: int, optional
            Number of iterations to run using kz regularization filter
        kz_regularization_gamma, float, optional
            kz regularization strength
        identical_slices_iter: int, optional
            Number of iterations to run using identical slices
        store_iterations: bool, optional
            If True, reconstructed objects and probes are stored at each iteration
        progress_bar: bool, optional
            If True, reconstruction progress is displayed
        reset: bool, optional
            If True, previous reconstructions are ignored

        Returns
        --------
        self: MultislicePtychographicReconstruction
            Self to accommodate chaining
        """
        asnumpy = self._asnumpy
        xp = self._xp

        # Reconstruction method

        if reconstruction_method == "generalized-projection":
            if np.array(reconstruction_parameter).shape != (3,):
                raise ValueError(
                    (
                        "reconstruction_parameter must be a list of three numbers "
                        "when using `reconstriction_method`=generalized-projection."
                    )
                )

            use_projection_scheme = True
            projection_a, projection_b, projection_c = reconstruction_parameter
            step_size = None
        elif (
            reconstruction_method == "DM_AP"
            or reconstruction_method == "difference-map_alternating-projections"
        ):
            if reconstruction_parameter < 0.0 or reconstruction_parameter > 1.0:
                raise ValueError("reconstruction_parameter must be between 0-1.")

            use_projection_scheme = True
            projection_a = -reconstruction_parameter
            projection_b = 1
            projection_c = 1 + reconstruction_parameter
            step_size = None
        elif (
            reconstruction_method == "RAAR"
            or reconstruction_method == "relaxed-averaged-alternating-reflections"
        ):
            if reconstruction_parameter < 0.0 or reconstruction_parameter > 1.0:
                raise ValueError("reconstruction_parameter must be between 0-1.")

            use_projection_scheme = True
            projection_a = 1 - 2 * reconstruction_parameter
            projection_b = reconstruction_parameter
            projection_c = 2
            step_size = None
        elif (
            reconstruction_method == "RRR"
            or reconstruction_method == "relax-reflect-reflect"
        ):
            if reconstruction_parameter < 0.0 or reconstruction_parameter > 2.0:
                raise ValueError("reconstruction_parameter must be between 0-2.")

            use_projection_scheme = True
            projection_a = -reconstruction_parameter
            projection_b = reconstruction_parameter
            projection_c = 2
            step_size = None
        elif (
            reconstruction_method == "SUPERFLIP"
            or reconstruction_method == "charge-flipping"
        ):
            use_projection_scheme = True
            projection_a = 0
            projection_b = 1
            projection_c = 2
            reconstruction_parameter = None
            step_size = None
        elif (
            reconstruction_method == "GD" or reconstruction_method == "gradient-descent"
        ):
            use_projection_scheme = False
            projection_a = None
            projection_b = None
            projection_c = None
            reconstruction_parameter = None
        else:
            raise ValueError(
                (
                    "reconstruction_method must be one of 'DM_AP' (or 'difference-map_alternating-projections'), "
                    "'RAAR' (or 'relaxed-averaged-alternating-reflections'), "
                    "'RRR' (or 'relax-reflect-reflect'), "
                    "'SUPERFLIP' (or 'charge-flipping'), "
                    f"or 'GD' (or 'gradient-descent'), not  {reconstruction_method}."
                )
            )

        if self._verbose:
            if max_batch_size is not None:
                if use_projection_scheme:
                    raise ValueError(
                        (
                            "Stochastic object/probe updating is inconsistent with 'DM_AP', 'RAAR', 'RRR', and 'SUPERFLIP'. "
                            "Use reconstruction_method='GD' or set max_batch_size=None."
                        )
                    )
                else:
                    print(
                        (
                            f"Performing {max_iter} iterations using the {reconstruction_method} algorithm, "
                            f"with normalization_min: {normalization_min} and step _size: {step_size}, "
                            f"in batches of max {max_batch_size} measurements."
                        )
                    )
            else:
                if reconstruction_parameter is not None:
                    if np.array(reconstruction_parameter).shape == (3,):
                        print(
                            (
                                f"Performing {max_iter} iterations using the {reconstruction_method} algorithm, "
                                f"with normalization_min: {normalization_min} and (a,b,c): {reconstruction_parameter}."
                            )
                        )
                    else:
                        print(
                            (
                                f"Performing {max_iter} iterations using the {reconstruction_method} algorithm, "
                                f"with normalization_min: {normalization_min} and α: {reconstruction_parameter}."
                            )
                        )
                else:
                    if step_size is not None:
                        print(
                            (
                                f"Performing {max_iter} iterations using the {reconstruction_method} algorithm, "
                                f"with normalization_min: {normalization_min}."
                            )
                        )
                    else:
                        print(
                            (
                                f"Performing {max_iter} iterations using the {reconstruction_method} algorithm, "
                                f"with normalization_min: {normalization_min} and step _size: {step_size}."
                            )
                        )

        # Batching
        shuffled_indices = np.arange(self._num_diffraction_patterns)
        unshuffled_indices = np.zeros_like(shuffled_indices)

        if max_batch_size is not None:
            xp.random.seed(seed_random)
        else:
            max_batch_size = self._num_diffraction_patterns

        # initialization
        if store_iterations and (not hasattr(self, "object_iterations") or reset):
            self.object_iterations = []
            self.probe_iterations = []
            self.error_iterations = []

        if reset:
            self._object = self._object_initial.copy()
            self._probe = self._probe_initial.copy()
            self._positions_px = self._positions_px_initial.copy()
            self._positions_px_fractional = self._positions_px - xp.round(
                self._positions_px
            )
            (
                self._vectorized_patch_indices_row,
                self._vectorized_patch_indices_col,
            ) = self._extract_vectorized_patch_indices()
            self._exit_waves = None
        elif reset is None:
            if hasattr(self, "error"):
                warnings.warn(
                    (
                        "Continuing reconstruction from previous result. "
                        "Use reset=True for a fresh start."
                    ),
                    UserWarning,
                )
            else:
                self._exit_waves = None

        # Probe support mask initialization
        x = xp.linspace(-1, 1, self._region_of_interest_shape[0], endpoint=False)
        y = xp.linspace(-1, 1, self._region_of_interest_shape[1], endpoint=False)
        xx, yy = xp.meshgrid(x, y, indexing="ij")
        self._probe_support_mask = xp.exp(
            -(
                (
                    (xx / probe_support_relative_radius) ** 2
                    + (yy / probe_support_relative_radius) ** 2
                )
                ** probe_support_supergaussian_degree
            )
        )

        # main loop
        for a0 in tqdmnd(
            max_iter,
            desc="Reconstructing object and probe",
            unit=" iter",
            disable=not progress_bar,
        ):
            error = 0.0

            # randomize
            if not use_projection_scheme:
                np.random.shuffle(shuffled_indices)
            unshuffled_indices[shuffled_indices] = np.arange(
                self._num_diffraction_patterns
            )
            positions_px = self._positions_px.copy()[shuffled_indices]

            for start, end in generate_batches(
                self._num_diffraction_patterns, max_batch=max_batch_size
            ):
                # batch indices
                self._positions_px = positions_px[start:end]
                self._positions_px_fractional = self._positions_px - xp.round(
                    self._positions_px
                )
                (
                    self._vectorized_patch_indices_row,
                    self._vectorized_patch_indices_col,
                ) = self._extract_vectorized_patch_indices()
                amplitudes = self._amplitudes[shuffled_indices[start:end]]

                # forward operator
                (
                    propagated_probes,
                    object_patches,
                    transmitted_probes,
                    self._exit_waves,
                    batch_error,
                ) = self._forward(
                    self._object,
                    self._probe,
                    amplitudes,
                    self._exit_waves,
                    use_projection_scheme,
                    projection_a,
                    projection_b,
                    projection_c,
                )

                # adjoint operator
                self._object, self._probe = self._adjoint(
                    self._object,
                    self._probe,
                    object_patches,
                    transmitted_probes,
                    self._exit_waves,
                    use_projection_scheme=use_projection_scheme,
                    step_size=step_size,
                    normalization_min=normalization_min,
                    fix_probe=a0 < fix_probe_iter,
                )

                # position correction
                if a0 >= fix_positions_iter:
                    positions_px[start:end] = self._position_correction(
                        self._object,
                        self._probe,
                        transmitted_probes,
                        amplitudes,
                        self._positions_px,
                        positions_step_size,
                    )

                error += batch_error

            # constraints
            self._positions_px = positions_px.copy()[unshuffled_indices]
            self._object, self._probe, self._positions_px = self._constraints(
                self._object,
                self._probe,
                self._positions_px,
                pure_phase_object=a0 < pure_phase_object_iter,
                fix_com=fix_com and a0 >= fix_probe_iter,
                fix_probe_fourier_amplitude=a0 < fix_probe_fourier_amplitude_iter,
                fix_positions=a0 < fix_positions_iter,
                global_affine_transformation=global_affine_transformation,
                gaussian_filter=a0 < gaussian_filter_iter
                and gaussian_filter_sigma is not None,
                gaussian_filter_sigma=gaussian_filter_sigma,
                butterworth_filter=a0 < butterworth_filter_iter
                and (q_lowpass is not None or q_highpass is not None),
                q_lowpass=q_lowpass,
                q_highpass=q_highpass,
                kz_regularization_filter=a0 < kz_regularization_filter_iter
                and kz_regularization_gamma is not None,
                kz_regularization_gamma=kz_regularization_gamma,
                identical_slices=a0 < identical_slices_iter,
            )

            if store_iterations:
                self.object_iterations.append(asnumpy(self._object.copy()))
                self.probe_iterations.append(asnumpy(self._probe.copy()))
                self.error_iterations.append(error.item())

        # store result
        self.object = asnumpy(self._object)
        self.probe = asnumpy(self._probe)
        self.error = error.item()

        return self

    def _visualize_last_iteration_figax(
        self,
        fig,
        object_ax,
        convergence_ax,
        cbar: bool,
        padding: int,
        relative_error: bool,
        **kwargs,
    ):
        """
        Displays last reconstructed object on a given fig/ax.

        Parameters
        --------
        fig: Figure
            Matplotlib figure object_ax lives in
        object_ax: Axes
            Matplotlib axes to plot reconstructed object in
        convergence_ax: Axes, optional
            Matplotlib axes to plot convergence plot in
        cbar: bool, optional
            If true, displays a colorbar
        """
        cmap = kwargs.get("cmap", "magma")
        kwargs.pop("cmap", None)

        summed_object_angle = np.sum(np.angle(self.object), axis=0)
        rotated_object = self._crop_rotate_object_fov(
            summed_object_angle, padding=padding
        )
        rotated_shape = rotated_object.shape

        extent = [
            0,
            self.sampling[1] * rotated_shape[1],
            self.sampling[0] * rotated_shape[0],
            0,
        ]

        im = object_ax.imshow(
            rotated_object,
            extent=extent,
            cmap=cmap,
            **kwargs,
        )

        if cbar:
            divider = make_axes_locatable(object_ax)
            ax_cb = divider.append_axes("right", size="5%", pad="2.5%")
            fig.add_axes(ax_cb)
            fig.colorbar(im, cax=ax_cb)

        if convergence_ax is not None and hasattr(self, "error_iterations"):
            errors = np.array(self.error_iterations)
            kwargs.pop("vmin", None)
            kwargs.pop("vmax", None)
            errors = self.error_iterations

            if relative_error:
                convergence_ax.semilogy(
                    np.arange(errors.shape[0]), errors / errors[0], **kwargs
                )
            else:
                convergence_ax.semilogy(np.arange(errors.shape[0]), errors, **kwargs)

    def _visualize_last_iteration(
        self,
        cbar: bool,
        plot_convergence: bool,
        plot_probe: bool,
        object_mode: str,
        padding: int,
        relative_error: bool,
        **kwargs,
    ):
        """
        Displays last reconstructed object and probe iterations.

        Parameters
        --------
        plot_convergence: bool, optional
            If true, the RMS error plot is displayed
        cbar: bool, optional
            If true, displays a colorbar
        plot_probe: bool
            If true, the reconstructed probe intensity is also displayed
        object_mode: str
            Specifies the attribute of the object to plot.
            One of 'phase', 'amplitude', 'intensity'
        relative_error: bool
            Sets the error to be relative to the first iteration.
            TODO - update to be relative to empty object wave error (RMS of all measurements).

        """
        figsize = kwargs.get("figsize", (8, 5))
        cmap = kwargs.get("cmap", "magma")
        kwargs.pop("figsize", None)
        kwargs.pop("cmap", None)

        summed_object_angle = np.sum(np.angle(self.object), axis=0)
        rotated_object = self._crop_rotate_object_fov(
            summed_object_angle, padding=padding
        )
        rotated_shape = rotated_object.shape

        extent = [
            0,
            self.sampling[1] * rotated_shape[1],
            self.sampling[0] * rotated_shape[0],
            0,
        ]

        probe_extent = [
            0,
            self.sampling[1] * self._region_of_interest_shape[1],
            self.sampling[0] * self._region_of_interest_shape[0],
            0,
        ]

        if plot_convergence:
            if plot_probe:
                spec = GridSpec(
                    ncols=2,
                    nrows=2,
                    height_ratios=[4, 1],
                    hspace=0.15,
                    width_ratios=[
                        (extent[1] / extent[2]) / (probe_extent[1] / probe_extent[2]),
                        1,
                    ],
                    wspace=0.35,
                )
            else:
                spec = GridSpec(ncols=1, nrows=2, height_ratios=[4, 1], hspace=0.15)
        else:
            if plot_probe:
                spec = GridSpec(
                    ncols=2,
                    nrows=1,
                    width_ratios=[
                        (extent[1] / extent[2]) / (probe_extent[1] / probe_extent[2]),
                        1,
                    ],
                    wspace=0.35,
                )
            else:
                spec = GridSpec(ncols=1, nrows=1)

        fig = plt.figure(figsize=figsize)

        if plot_probe:
            # Object
            ax = fig.add_subplot(spec[0, 0])
            if object_mode == "phase":
                im = ax.imshow(
                    rotated_object,
                    extent=extent,
                    cmap=cmap,
                    **kwargs,
                )
            else:
                raise NotImplementedError()

            ax.set_ylabel("x [A]")
            ax.set_xlabel("y [A]")
            ax.set_title(f"Reconstructed object {object_mode}")

            if cbar:
                divider = make_axes_locatable(ax)
                ax_cb = divider.append_axes("right", size="5%", pad="2.5%")
                fig.add_axes(ax_cb)
                fig.colorbar(im, cax=ax_cb)

            # Probe
            kwargs.pop("vmin", None)
            kwargs.pop("vmax", None)
            ax = fig.add_subplot(spec[0, 1])
            im = ax.imshow(
                np.abs(self.probe) ** 2,
                extent=probe_extent,
                cmap="Greys_r",
                **kwargs,
            )
            ax.set_ylabel("x [A]")
            ax.set_xlabel("y [A]")
            ax.set_title("Reconstructed probe intensity")

            if cbar:
                divider = make_axes_locatable(ax)
                ax_cb = divider.append_axes("right", size="5%", pad="2.5%")
                fig.add_axes(ax_cb)
                fig.colorbar(im, cax=ax_cb)

        else:
            ax = fig.add_subplot(spec[0])
            if object_mode == "phase":
                im = ax.imshow(
                    rotated_object,
                    extent=extent,
                    cmap=cmap,
                    **kwargs,
                )
            else:
                raise NotImplementedError()
            ax.set_ylabel("x [A]")
            ax.set_xlabel("y [A]")
            ax.set_title(f"Reconstructed object {object_mode}")

            if cbar:
                divider = make_axes_locatable(ax)
                ax_cb = divider.append_axes("right", size="5%", pad="2.5%")
                fig.add_axes(ax_cb)
                fig.colorbar(im, cax=ax_cb)

        if plot_convergence and hasattr(self, "error_iterations"):
            kwargs.pop("vmin", None)
            kwargs.pop("vmax", None)
            errors = np.array(self.error_iterations)
            if plot_probe:
                ax = fig.add_subplot(spec[1, :])
            else:
                ax = fig.add_subplot(spec[1])
            if relative_error:
                ax.semilogy(np.arange(errors.shape[0]), errors / errors[0], **kwargs)
                ax.set_ylabel("Log Rel. RMS error")
            else:
                ax.semilogy(np.arange(errors.shape[0]), errors, **kwargs)
                ax.set_ylabel("Log RMS error")
            ax.set_xlabel("Iteration Number")
            ax.yaxis.tick_right()

        fig.suptitle(f"RMS error: {self.error:.3e}")
        spec.tight_layout(fig)

    def _visualize_all_iterations(
        self,
        cbar: bool,
        plot_convergence: bool,
        plot_probe: bool,
        iterations_grid: Tuple[int, int],
        object_mode: str,
        padding: int,
        relative_error: bool,
        **kwargs,
    ):
        """
        Displays all reconstructed object and probe iterations.

        Parameters
        --------
        plot_convergence: bool, optional
            If true, the RMS error plot is displayed
        iterations_grid: Tuple[int,int]
            Grid dimensions to plot reconstruction iterations
        cbar: bool, optional
            If true, displays a colorbar
        plot_probe: bool
            If true, the reconstructed probe intensity is also displayed
        object_mode: str
            Specifies the attribute of the object to plot.
            One of 'phase', 'amplitude', 'intensity'
        relative_error: bool
            Sets the error to be relative to the first iteration.
            TODO - update to be relative to empty object wave error (RMS of all measurements).

        """
        if iterations_grid == "auto":
            iterations_grid = (2, 4)
        else:
            if plot_probe and iterations_grid[0] != 2:
                raise ValueError()

        figsize = kwargs.get("figsize", (12, 7))
        cmap = kwargs.get("cmap", "magma")
        kwargs.pop("figsize", None)
        kwargs.pop("cmap", None)

        errors = np.array(self.error_iterations)
        objects = [
            self._crop_rotate_object_fov(np.sum(np.angle(obj), axis=0), padding=padding)
            for obj in self.object_iterations
        ]

        if plot_probe:
            total_grids = (np.prod(iterations_grid) / 2).astype("int")
            probes = self.probe_iterations
        else:
            total_grids = np.prod(iterations_grid)
        max_iter = len(objects) - 1
        grid_range = range(0, max_iter + 1, max_iter // (total_grids - 1))

        extent = [
            0,
            self.sampling[1] * objects[0].shape[1],
            self.sampling[0] * objects[0].shape[0],
            0,
        ]

        probe_extent = [
            0,
            self.sampling[1] * self._region_of_interest_shape[1],
            self.sampling[0] * self._region_of_interest_shape[0],
            0,
        ]

        if plot_convergence:
            if plot_probe:
                spec = GridSpec(ncols=1, nrows=3, height_ratios=[4, 4, 1], hspace=0)
            else:
                spec = GridSpec(ncols=1, nrows=2, height_ratios=[4, 1], hspace=0)
        else:
            if plot_probe:
                spec = GridSpec(ncols=1, nrows=2)
            else:
                spec = GridSpec(ncols=1, nrows=1)

        fig = plt.figure(figsize=figsize)

        grid = ImageGrid(
            fig,
            spec[0],
            nrows_ncols=(1, iterations_grid[1]) if plot_probe else iterations_grid,
            axes_pad=(0.75, 0.5) if cbar else 0.5,
            cbar_mode="each" if cbar else None,
            cbar_pad="2.5%" if cbar else None,
        )

        for n, ax in enumerate(grid):
            if object_mode == "phase":
                im = ax.imshow(
                    objects[grid_range[n]],
                    extent=extent,
                    cmap=cmap,
                    **kwargs,
                )
                ax.set_title(f"Iter: {grid_range[n]} Phase")
            else:
                raise NotImplementedError()

            ax.set_ylabel("x [A]")
            ax.set_xlabel("y [A]")
            if cbar:
                grid.cbar_axes[n].colorbar(im)

        if plot_probe:
            kwargs.pop("vmin", None)
            kwargs.pop("vmax", None)
            grid = ImageGrid(
                fig,
                spec[1],
                nrows_ncols=(1, iterations_grid[1]),
                axes_pad=(0.75, 0.5) if cbar else 0.5,
                cbar_mode="each" if cbar else None,
                cbar_pad="2.5%" if cbar else None,
            )

            for n, ax in enumerate(grid):
                im = ax.imshow(
                    np.abs(probes[grid_range[n]]) ** 2,
                    extent=probe_extent,
                    cmap="Greys_r",
                    **kwargs,
                )
                ax.set_title(f"Iter: {grid_range[n]} Probe")

                ax.set_ylabel("x [A]")
                ax.set_xlabel("y [A]")

                if cbar:
                    grid.cbar_axes[n].colorbar(im)

        if plot_convergence:
            kwargs.pop("vmin", None)
            kwargs.pop("vmax", None)
            if plot_probe:
                ax2 = fig.add_subplot(spec[2])
            else:
                ax2 = fig.add_subplot(spec[1])
            if relative_error:
                ax2.semilogy(np.arange(errors.shape[0]), errors / errors[0], **kwargs)
                ax2.set_ylabel("Log Rel. RMS error")
            else:
                ax2.semilogy(np.arange(errors.shape[0]), errors, **kwargs)
                ax2.set_ylabel("Log RMS error")
            ax2.set_xlabel("Iteration Number")
            ax2.yaxis.tick_right()

        spec.tight_layout(fig)

    def visualize(
        self,
        iterations_grid: Tuple[int, int] = None,
        plot_convergence: bool = True,
        plot_probe: bool = True,
        object_mode: str = "phase",
        cbar: bool = True,
        padding: int = 0,
        relative_error: bool = True,
        **kwargs,
    ):
        """
        Displays reconstructed object and probe.

        Parameters
        --------
        plot_convergence: bool, optional
            If true, the RMS error plot is displayed
        iterations_grid: Tuple[int,int]
            Grid dimensions to plot reconstruction iterations
        cbar: bool, optional
            If true, displays a colorbar
        plot_probe: bool
            If true, the reconstructed probe intensity is also displayed
        object_mode: str
            Specifies the attribute of the object to plot.
            One of 'phase', 'amplitude', 'intensity'
        relative_error: bool
            Sets the error to be relative to the first iteration.
            TODO - update to be relative to empty object wave error (RMS of all measurements).

        Returns
        --------
        self: PtychographicReconstruction
            Self to accommodate chaining
        """

        if (
            object_mode != "phase"
            and object_mode != "amplitude"
            and object_mode != "intensity"
        ):
            raise ValueError(
                (
                    "object_mode needs to be one of 'phase', 'amplitude', "
                    f"or 'intensity', not {object_mode}"
                )
            )

        if iterations_grid is None:
            self._visualize_last_iteration(
                plot_convergence=plot_convergence,
                plot_probe=plot_probe,
                object_mode=object_mode,
                cbar=cbar,
                padding=padding,
                relative_error=relative_error,
                **kwargs,
            )
        else:
            self._visualize_all_iterations(
                plot_convergence=plot_convergence,
                iterations_grid=iterations_grid,
                plot_probe=plot_probe,
                object_mode=object_mode,
                cbar=cbar,
                padding=padding,
                relative_error=relative_error,
                **kwargs,
            )

        return self