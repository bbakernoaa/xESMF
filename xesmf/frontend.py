'''
Frontend for xESMF, exposed to users.
'''

import numpy as np
import xarray as xr
import os

from .backend import (esmf_grid, add_corner, esmf_regrid_build,
                      esmf_regrid_finalize)

from .smm import read_weights, apply_weights


def get_latlon_name(ds, boundary=False):
    if boundary:
        try:
            # COARDS netCDF complaint
            if 'lat_b' in ds.variables:
                lat_name = 'lat_b'
                lon_name = 'lon_b'
            # NETCDF CF 1.6 complaint
            elif 'latitude_b' in ds.variables:
                lat_name = 'latitude_b'
                lon_name = 'longitude_b'
            else:
                raise ValueError
        except ValueError:
            print(
                """Must have coordinates compliant with NETCDF COARDS or CF conventions"""
            )
    else:
        try:
            # COARDS netCDF complaint
            if 'lat' in ds.variables:
                lat_name = 'lat'
                lon_name = 'lon'
            # NETCDF CF 1.6 complaint
            elif 'latitude' in ds.variables:
                lat_name = 'latitude'
                lon_name = 'longitude'
            else:
                raise ValueError
        except ValueError:
            print(
                'Must have coordinates compliant with NETCDF COARDS or CF conventions'
            )
    return lat_name, lon_name


def as_2d_mesh(lon, lat):

    if (lon.ndim, lat.ndim) == (2, 2):
        assert lon.shape == lat.shape, 'lon and lat should have same shape'
    elif (lon.ndim, lat.ndim) == (1, 1):
        lon, lat = np.meshgrid(lon, lat)
    else:
        raise ValueError('lon and lat should be both 1D or 2D')

    return lon, lat


def ds_to_ESMFgrid(ds,
                   need_bounds=False,
                   lat=None,
                   lon=None,
                   lat_b=None,
                   lon_b=None,
                   periodic=None,
                   append=None):
    '''
    Convert xarray DataSet or dictionary to ESMF.Grid object.

    Parameters
    ----------
    ds : xarray DataSet or dictionary
        Contains variables ``lon``, ``lat``,
        and optionally ``lon_b``, ``lat_b`` if need_bounds=True.

        Shape should be ``(Nlat, Nlon)`` or ``(Ny, Nx)``,
        as normal C or Python ordering. Will be then tranposed to F-ordered.

    need_bounds : bool, optional
        Need cell boundary values?

    periodic : bool, optional
        Periodic in longitude?

    Returns
    -------
    grid : ESMF.Grid object

    '''

    # use np.asarray(dr) instead of dr.values, so it also works for dictionary
    if lat is None and lon is None:
        lon_name, lat_name = get_latlon_name(ds)
    else:
        lat_name = lat
        lon_name = lon
    lon = np.asarray(ds[lon_name])
    lat = np.asarray(ds[lat_name])
    lon, lat = as_2d_mesh(lon, lat)

    # tranpose the arrays so they become Fortran-ordered
    grid = esmf_grid(lon.T, lat.T, periodic=periodic)

    if need_bounds:
        if lat_b is None and lon_b is None:
            lon_b, lat_b = get_latlon_name(ds, boundary=True)
        lon_b = np.asarray(ds[lon_b])
        lat_b = np.asarray(ds[lat_b])
        lon_b, lat_b = as_2d_mesh(lon_b, lat_b)
        add_corner(grid, lon_b.T, lat_b.T)

    return grid, lon.shape


class Regridder(object):
    def __init__(self,
                 ds_in,
                 ds_out,
                 method,
                 periodic=False,
                 filename=None,
                 reuse_weights=False,
                 lat_in=None,
                 lon_in=None,
                 lat_b_in=None,
                 lon_b_in=None,
                 lat_out=None,
                 lon_out=None,
                 lat_b_out=None,
                 lon_b_out=None):
        """
        Make xESMF regridder

        Parameters
        ----------
        ds_in, ds_out : xarray DataSet, or dictionary
            Contain input and output grid coordinates. Look for variables
            ``lon``, ``lat``, and optionally ``lon_b``, ``lat_b`` for
            conservative method.

            Shape can be 1D (Nlon,) and (Nlat,) for rectilinear grids,
            or 2D (Ny, Nx) for general curvilinear grids.
            Shape of bounds should be (N+1,) or (Ny+1, Nx+1).

        method : str
            Regridding method. Options are

            - 'bilinear'
            - 'conservative', **need grid corner information**
            - 'patch'
            - 'nearest_s2d'
            - 'nearest_d2s'

        periodic : bool, optional
            Periodic in longitude? Default to False.
            Only useful for global grids with non-conservative regridding.
            Will be forced to False for conservative regridding.

        filename : str, optional
            Name for the weight file. The default naming scheme is::

                {method}_{Ny_in}x{Nx_in}_{Ny_out}x{Nx_out}.nc

            e.g. bilinear_400x600_300x400.nc

        reuse_weights : bool, optional
            Whether to read existing weight file to save computing time.
            False by default (i.e. re-compute, not reuse).

        lat_in : string, optional
            Latitude name in ds_in xarray.DataArray.  If none it will try to
            detect if the DataArray is netCDF CF 1.6 or COARDS compliant

        lon_in : string, optional
            Latitude name in ds_in xarray.DataArray.  If none it will try to
             if the DataArray is netCDF CF 1.6 or COARDS compliant

        lat_b_in : string, optional
            Latitude name in ds_in xarray.DataArray.  If none it will try to
            detect if the DataArray is netCDF CF 1.6 or COARDS compliant

        lon_b_in : string, optional
            Latitude name in ds_in xarray.DataArray.  If none it will try to
            detect if the DataArray is netCDF CF 1.6 or COARDS compliant

        lat_out : string, optional
            Latitude name in ds_in xarray.DataArray.  If none it will try to
            detect if the DataArray is netCDF CF 1.6 or COARDS compliant

        lon_out : string, optional
            Latitude name in ds_in xarray.DataArray.  If none it will try to
            detect if the DataArray is netCDF CF 1.6 or COARDS compliant

        lat_b_out : string, optional
            Latitude name in ds_in xarray.DataArray.  If none it will try to
            detect if the DataArray is netCDF CF 1.6 or COARDS compliant

        lon_b_out : string, optional
            Latitude name in ds_in xarray.DataArray.  If none it will try to
            detectif the DataArray is netCDF CF 1.6 or COARDS compliant

        Returns
        -------
        regridder : xESMF regridder object

        """

        # record basic switches
        if method == 'conservative':
            self.need_bounds = True
            periodic = False  # bound shape will not be N+1 for periodic grid
        else:
            self.need_bounds = False

        self.method = method
        self.periodic = periodic
        self.reuse_weights = reuse_weights

        # get latitude and longitude names for ds_in and ds_out
        if lat_in is None and lon_in is None:
            self.lat_name_in, self.lon_name_in = get_latlon_name(ds_in)
        else:
            self.lat_name_in = lat_in
            self.lon_name_in = lon_in
        if lat_b_in is None and lon_b_in is None:
            self.lat_b_name_in, self.lon_b_name_in = get_latlon_name(
                ds_in, boundary=True)
        else:
            self.lat_b_name_in = lat_b_in
            self.lon_b_name_in = lon_b_in
        if lat_out is None and lon_out is None:
            self.lat_name_out, self.lon_name_out = get_latlon_name(ds_out)
        else:
            self.lat_name_out = lat_out
            self.lon_name_out = lon_out
        if lat_b_out is None and lon_b_out is None:
            self.lat_b_name_out, self.lon_b_name_out = get_latlon_name(
                ds_out, boundary=True)
        else:
            self.lat_b_name_out = lat_b_out
            self.lon_b_name_out = lon_b_out

        # construct ESMF grid, with some shape checking
        self._grid_in, shape_in = ds_to_ESMFgrid(
            ds_in,
            need_bounds=self.need_bounds,
            periodic=periodic,
            lat=self.lat_name_in,
            lon=self.lon_name_in,
            lat_b=self.lat_b_name_in,
            lon_b=self.lon_b_name_in)
        self._grid_out, shape_out = ds_to_ESMFgrid(
            ds_out,
            need_bounds=self.need_bounds,
            lat=self.lat_name_out,
            lon=self.lon_name_out,
            lat_b=self.lat_b_name_out,
            lon_b=self.lon_b_name_out)

        # record output grid and metadata
        self._lon_out = np.asarray(ds_out[self.lon_name_out])
        self._lat_out = np.asarray(ds_out[self.lat_name_out])

        if self._lon_out.ndim == 2:
            try:
                self.lon_dim = self.lat_dim = ds_out[self.lon_name_out].dims
            except:
                self.lon_dim = self.lat_dim = ('y', 'x')

            self.horiz_dims = self.lon_dim

        elif self._lon_out.ndim == 1:
            try:
                self.lon_dim = ds_out[self.lon_name_out].dims
                self.lat_dim = ds_out[self.lat_name_out].dims
            except:
                self.lon_dim = self.lon_name_out
                self.lat_dim = self.lat_name_out

            self.horiz_dims = (self.lat_dim, self.lon_dim)

        # get grid shape information
        # Use (Ny, Nx) instead of (Nlat, Nlon),
        # because ds can be general curvilinear grids.
        # For rectilinear grids, (Ny, Nx) == (Nlat, Nlon)
        self.Ny_in, self.Nx_in = shape_in
        self.Ny_out, self.Nx_out = shape_out
        self.N_in = self.Ny_in * self.Nx_in
        self.N_out = self.Ny_out * self.Nx_out

        if filename is None:
            self.filename = self._get_default_filename()
        else:
            self.filename = filename

        # get weight matrix
        self._write_weight_file()
        self.A = read_weights(self.filename, self.N_in, self.N_out)

    def _get_default_filename(self):
        # e.g. bilinear_400x600_300x400.nc
        filename = ('{0}_{1}x{2}_{3}x{4}'.format(
            self.method, self.Ny_in, self.Nx_in, self.Ny_out, self.Nx_out))
        if self.periodic:
            filename += '_peri.nc'
        else:
            filename += '.nc'

        return filename

    def _write_weight_file(self):

        if os.path.exists(self.filename):
            if self.reuse_weights:
                print('Reuse existing file: {}'.format(self.filename))
                return  # do not compute it again, just read it
            else:
                print(
                    'Overwrite existing file: {} \n'.format(self.filename),
                    'You can set reuse_weights=True to save computing time.')
                os.remove(self.filename)
        else:
            print('Create weight file: {}'.format(self.filename))

        regrid = esmf_regrid_build(
            self._grid_in, self._grid_out, self.method, filename=self.filename)
        esmf_regrid_finalize(regrid)  # only need weights, not regrid object

    def clean_weight_file(self):
        """
        Remove the offline weight file on disk.

        To save the time on re-computing weights, you can just keep the file,
        and set "reuse_weights=True" when initializing the regridder next time.
        """
        if os.path.exists(self.filename):
            print("Remove file {}".format(self.filename))
            os.remove(self.filename)
        else:
            print("File {} is already removed.".format(self.filename))

    def __repr__(self):
        info = ('xESMF Regridder \n'
                'Regridding algorithm:       {} \n'
                'Weight filename:            {} \n'
                'Reuse pre-computed weights? {} \n'
                'Input grid shape:           {} \n'
                'Output grid shape:          {} \n'
                'Output grid dimension name: {} \n'
                'Periodic in longitude?      {}'.format(
                    self.method, self.filename, self.reuse_weights,
                    (self.Ny_in, self.Nx_in), (self.Ny_out, self.Nx_out),
                    self.horiz_dims, self.periodic))

        return info

    def __call__(self, a):
        """
        Shortcut for ``regrid_numpy()`` and ``regrid_dataarray()``.

        Parameters
        ----------
        a : xarray DataArray or numpy array

        Returns
        -------
        xarray DataArray or numpy array
            Regridding results. Type depends on input.
        """
        # TODO: DataSet support

        if isinstance(a, np.ndarray):
            regrid_func = self.regrid_numpy
        elif isinstance(a, xr.DataArray):
            regrid_func = self.regrid_dataarray
        else:
            raise TypeError("input must be numpy array or xarray DataArray!")

        return regrid_func(a)

    def regrid_numpy(self, indata):
        """
        Regrid pure numpy array. Shape requirement is the same as
        ``regrid_dataarray()``

        Parameters
        ----------
        indata : numpy array

        Returns
        -------
        outdata : numpy array

        """

        # check shape
        shape_horiz = indata.shape[-2:]  # the rightmost two dimensions
        assert shape_horiz == (self.Ny_in, self.Nx_in), (
            'The horizontal shape of input data is {}, different from that of'
            'the regridder {}!'.format(shape_horiz, (self.Ny_in, self.Nx_in)))

        outdata = apply_weights(self.A, indata, self.Ny_out, self.Nx_out)
        return outdata

    def regrid_dataarray(self, dr_in):
        """
        Regrid xarray DataArray, track metadata.

        Parameters
        ----------
        dr_in : xarray DataArray
            The rightmost two dimensions must be the same as ``ds_in``.
            Can have arbitrary additional dimensions.

            Examples of valid shapes

            - (Nlat, Nlon), if ``ds_in`` has shape (Nlat, Nlon)
            - (N2, N1, Ny, Nx), if ``ds_in`` has shape (Ny, Nx)

        Returns
        -------
        dr_out : xarray DataArray
            On the same horizontal grid as ``ds_out``,
            with extra dims in ``dr_in``.

            Assuming ``ds_out`` has the shape of (Ny_out, Nx_out),
            examples of returning shapes are

            - (Ny_out, Nx_out), if ``dr_in`` is 2D
            - (N2, N1, Ny_out, Nx_out), if ``dr_in`` has shape
              (N2, N1, Ny, Nx)

        """

        # apply regridding to pure numpy array
        outdata = self.regrid_numpy(dr_in.values)

        # track metadata
        varname = dr_in.name
        extra_dims = dr_in.dims[0:-2]

        dr_out = xr.DataArray(
            outdata, dims=extra_dims + self.horiz_dims, name=varname)
        dr_out.coords[self.lon_name_out] = xr.DataArray(
            self._lon_out, dims=self.lon_dim)
        dr_out.coords[self.lat_name_out] = xr.DataArray(
            self._lat_out, dims=self.lat_dim)

        # append extra dimension coordinate value
        for dim in extra_dims:
            dr_out.coords[dim] = dr_in.coords[dim]

        dr_out.attrs['regrid_method'] = self.method

        return dr_out

    def regrid_dataset(self, ds_in):
        raise NotImplementedError("Only support regrid_dataarray() for now.")
