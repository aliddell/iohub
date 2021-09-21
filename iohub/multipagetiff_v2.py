import numpy as np
import os
import zarr
from tifffile import TiffFile
import tifffile as tiff
from copy import copy
import logging
import glob
import warnings

from waveorder.io.reader_interface import ReaderInterface


class MicromanagerOmeTiffReader(ReaderInterface):

    def __init__(self, folder: str, extract_data: bool = False):
        """

        Parameters
        ----------
        folder:         (str) folder or file containing all ome-tiff files
        extract_data:   (bool) True if ome_series should be extracted immediately

        """

        # Add Initial Checks
        if len(glob.glob(os.path.join(folder, '*.ome.tif'))) == 0:
            raise ValueError('Specific input contains no ome.tif files, please specify a valid input directory')

        # ignore tiffile warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', tiff)

        self.data_directory = folder
        self.files = glob.glob(os.path.join(self.data_directory, '*.ome.tif'))

        # Generate Data Specific Properties
        self.coords = None
        self.coord_map = dict()
        self.pos_names = []
        self.dim_order = None
        self.p_dim = None
        self.t_dim = None
        self.c_dim = None
        self.z_dim = None
        self.position_arrays = dict()
        self.positions = 0
        self.frames = 0
        self.channels = 0
        self.slices = 0
        self.height = 0
        self.width = 0
        self._set_dtype()
        # print(f'Found Dataset {self.save_name} w/ dimensions (P, T, C, Z, Y, X): {self.dim}')

        self.mm_meta = None
        self.stage_positions = 0
        self.z_step_size = None
        self.channel_names = []

        self._missing_dims = None

        self._set_mm_meta()
        self._gather_index_maps()

        if extract_data:
            for i in range(self.positions):
                self._create_position_array(i)

    def _gather_index_maps(self):
        """
        Will return a dictionary of {coord: (filepath, page, byte_offset)} of length(N_Images) to later query

        Returns
        -------

        """

        for file in self.files:
            tf = TiffFile(file)
            meta = tf.micromanager_metadata['IndexMap']

            for page in range(len(meta['Channel'])):
                coord = [0, 0, 0, 0]
                coord[0] = meta['Position'][page]
                coord[1] = meta['Frame'][page]
                coord[2] = meta['Channel'][page]
                coord[3] = meta['Slice'][page]
                offset = self._get_byte_offset(tf, page)
                self.coord_map[tuple(coord)] = (file, page, offset)

                # update dimensions
                if coord[0] > self.positions:
                    self.positions = coord[0]

                if coord[1] > self.frames:
                    self.frames = coord[1]

                if coord[2] > self.channels:
                    self.channels = coord[2]

                if coord[3] > self.slices:
                    self.slices = coord[2]


    def _get_byte_offset(self, tiff_file, page):
        """
        Gets the byte offset from the tiff tag metadata

        Parameters
        ----------
        tiff_file:          (Tiff-File object) Opened tiff file
        page:               (int) Page to look at for the tag

        Returns
        -------
        byte offset:        (int) byte offset for the image array

        """

        for tag in tiff_file.pages[page].tags.values():
            if 'StripOffset' in tag.name:
                return tag.value[0]
            else:
                continue

    def _set_mm_meta(self):
        """
        assign image metadata from summary metadata

        Returns
        -------

        """
        with TiffFile(self.files[0]) as tif:
            self.mm_meta = tif.micromanager_metadata

            mm_version = self.mm_meta['Summary']['MicroManagerVersion']
            if 'beta' in mm_version:
                if self.mm_meta['Summary']['Positions'] > 1:
                    self.stage_positions = []

                    for p in range(len(self.mm_meta['Summary']['StagePositions'])):
                        pos = self._simplify_stage_position_beta(self.mm_meta['Summary']['StagePositions'][p])
                        self.stage_positions.append(pos)
                # self.channel_names = 'Not Listed'

            elif mm_version == '1.4.22':
                for ch in self.mm_meta['Summary']['ChNames']:
                    self.channel_names.append(ch)

            else:
                if self.mm_meta['Summary']['Positions'] > 1:
                    self.stage_positions = []

                    for p in range(self.mm_meta['Summary']['Positions']):
                        pos = self._simplify_stage_position(self.mm_meta['Summary']['StagePositions'][p])
                        self.stage_positions.append(pos)

                for ch in self.mm_meta['Summary']['ChNames']:
                    self.channel_names.append(ch)

            # dimensions based on mm metadata do not reflect final written dimensions
            # these will change after data is loaded
            self.z_step_size = self.mm_meta['Summary']['z-step_um']
            self.height = self.mm_meta['Summary']['Height']
            self.width = self.mm_meta['Summary']['Width']
            self.frames = self.mm_meta['Summary']['Frames']
            self.slices = self.mm_meta['Summary']['Slices']
            self.channels = self.mm_meta['Summary']['Channels']

            # Reverse the dimension order and gather dimension indices
            self.dim_order = self.mm_meta['Summary']['AxisOrder']
            self.dim_order.reverse()
            self.p_dim = self.dim_order.index('position')
            self.t_dim = self.dim_order.index('time')
            self.c_dim = self.dim_order.index('channel')
            self.z_dim = self.dim_order.index('z')

    def _simplify_stage_position(self, stage_pos: dict):
        """
        flattens the nested dictionary structure of stage_pos and removes superfluous keys

        Parameters
        ----------
        stage_pos:      (dict) dictionary containing a single position's device info

        Returns
        -------
        out:            (dict) flattened dictionary
        """

        out = copy(stage_pos)
        out.pop('DevicePositions')
        for dev_pos in stage_pos['DevicePositions']:
            out.update({dev_pos['Device']: dev_pos['Position_um']})
        return out

    def _simplify_stage_position_beta(self, stage_pos: dict):
        """
        flattens the nested dictionary structure of stage_pos and removes superfluous keys
        for MM2.0 Beta versions

        Parameters
        ----------
        stage_pos:      (dict) dictionary containing a single position's device info

        Returns
        -------
        new_dict:       (dict) flattened dictionary

        """

        new_dict = {}
        new_dict['Label'] = stage_pos['label']
        new_dict['GridRow'] = stage_pos['gridRow']
        new_dict['GridCol'] = stage_pos['gridCol']

        for sub in stage_pos['subpositions']:
            values = []
            for field in ['x', 'y', 'z']:
                if sub[field] != 0:
                    values.append(sub[field])
            if len(values) == 1:
                new_dict[sub['stageName']] = values[0]
            else:
                new_dict[sub['stageName']] = values

        return new_dict

    def _create_position_array(self, pos):
        """
        extract all series from ome-tiff and place into dict of (pos: zarr)

        Parameters
        ----------
        master_ome:     (str): full path to master OME-tiff

        Returns
        -------

        """

        timepoints, channels, slices = self._get_dimensions(pos)
        self.position_arrays[pos] = zarr.empty(shape=(timepoints, channels, slices, self.height, self.width),
                                               chunks=(1, 1, 1, self.height, self.width),
                                               dtype=self.dtype)

        for t in range(timepoints):
            for c in range(channels):
                for z in range(slices):
                    self.position_arrays[pos][t, c, z, :, :] = self._get_image(pos, t, c, z)

    def _set_dtype(self):
        """
        gets the datatype from any image plane metadata

        Returns
        -------

        """

        tf = tiff.TiffFile(self.files[0])

        self.dtype = tf.pages[0].dtype

    def _get_dimensions(self, position):

        t = 0
        c = 0
        z = 0

        for tup in self.coord_map.keys():
            if position != tup[0]:
                continue
            else:
                if tup[1] > t:
                    t = tup[1]
                if tup[2] > c:
                    c = tup[2]
                if tup[3] > z:
                    z = tup[3]

        return t, c, z

    def _get_image(self, p, t, c, z):

        coord_key = (p, t, c, z)
        coord = self.coord_map[coord_key] # (file, page, offset)

        return np.memmap(coord[0], dtype=self.dtype, mode='r', offset=coord[2], shape=(self.height, self.width))

    def get_zarr(self, position):
        """
        return a zarr array for a given position

        Parameters
        ----------
        position:       (int) position (aka ome-tiff scene)

        Returns
        -------
        position:       (zarr.array)

        """
        if position not in self.position_arrays.keys():
            self._create_position_array(position)
        return self.position_arrays[position]

    def get_array(self, position):
        """
        return a numpy array for a given position

        Parameters
        ----------
        position:   (int) position (aka ome-tiff scene)

        Returns
        -------
        position:   (np.ndarray)

        """

        if position not in self.position_arrays.keys():
            self._create_position_array(position)

        return np.array(self.position_arrays[position])

    def get_num_positions(self):
        """
        get total number of scenes referenced in ome-tiff metadata

        Returns
        -------
        number of positions     (int)

        """
        return self.positions

    @property
    def shape(self):
        """
        return the underlying data shape as a tuple

        Returns
        -------
        (tuple) five elements of (frames, slices, channels, height, width)

        """
        return self.frames, self.channels, self.slices, self.height, self.width
