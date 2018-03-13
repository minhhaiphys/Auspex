# Copyright 2016 Raytheon BBN Technologies
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

from auspex.stream import DataStream, DataAxis, SweepAxis, DataStreamDescriptor, InputConnector, OutputConnector
from auspex.log import logger

import h5py
import numpy as np
import matplotlib.pyplot as plt

def load_from_HDF5(filename_or_fileobject, reshape=True, return_structured_array=True):
    data = {}
    descriptors = {}
    if isinstance(filename_or_fileobject, h5py.File):
        f = filename_or_fileobject
    else:
        f = h5py.File(filename_or_fileobject, 'r')
    for groupname in f:
        if groupname == "header":
            break # for now, ignore the header
        # Reconstruct the descriptor
        descriptor = DataStreamDescriptor()
        g = f[groupname]
        axis_refs = g['descriptor']
        for ref in reversed(axis_refs):
            ax = g[ref]
            if ax.attrs['unstructured']:
                # The entry for the main unstructured axis contains
                # references to the constituent axes.

                # The DataAxis expects points as tuples coordinates
                # in the form [(x1, y1), (x2, y2), ...].
                points = np.vstack([g[e] for e in ax[:]]).T
                names = [g[e].attrs["name"] for e in ax[:]]
                units = [g[e].attrs["unit"] for e in ax[:]]
                descriptor.add_axis(DataAxis(names, points=points, unit=units))
            else:
                name = ax.attrs['name']
                unit = ax.attrs['unit']
                points = ax[:]
                descriptor.add_axis(DataAxis(name, points=points, unit=unit))

        for attr_name in axis_refs.attrs.keys():
            descriptor.metadata[attr_name] = axis_refs.attrs[attr_name]

        col_names = list(g['data'].keys())
        if return_structured_array:
            dtype = [(g['data'][n].attrs['name'], g['data'][n].dtype.char) for n in col_names]
            length = g['data'][col_names[0]].shape[0]
            group_data = np.empty((length,), dtype=dtype)
            for cn in col_names:
                group_data[cn] = g['data'][cn]
        else:
            group_data = {n: g['data'][n][:] for n in col_names}

        if reshape:
            group_data = group_data.reshape(descriptor.dims())

        data[groupname] = group_data
        descriptors[groupname] = descriptor
    if not isinstance(filename_or_fileobject, h5py.File):
        f.close()
    return data, descriptors

def load_from_HDF5_legacy(filename_or_fileobject):
    data = {}
    if isinstance(filename_or_fileobject, h5py.File):
        f = filename_or_fileobject
    else:
        f = h5py.File(filename_or_fileobject, 'r')
    for groupname in f:
        # Reconstruct the descrciptor
        descriptor = DataStreamDescriptor()
        g = f[groupname]
        axis_refs = g['descriptor']
        for ref in reversed(axis_refs):
            ax = g[ref]
            if not 'unit' in ax.attrs:
                # Unstructured

                names = [k for k in ax.dtype.fields.keys()]
                units = [ax.attrs['unit_'+name] for name in names]
                points = ax[:]
                points = points.view(np.float32).reshape(points.shape + (-1,))
                descriptor.add_axis(DataAxis(names, points=points, unit=units))
            else:
                # Structured
                name = ax.attrs['NAME'].decode('UTF-8')
                unit = ax.attrs['unit']
                points = ax[:]
                descriptor.add_axis(DataAxis(name, points=points, unit=unit))

        for attr_name in axis_refs.attrs.keys():
            descriptor.metadata[attr_name] = axis_refs.attrs[attr_name]

        data[groupname] = g['data'][:]

    f.close()
    return data, descriptor

def load_data(filepath=None, showPlot=False):
    """
    Reimplement load_data.  Takes a file path or None and loads the data.

    """
    if filepath == None:
        filepath = get_file_name()
    elif filepath == 'latest':
        # TO-DO: load the latest data taken
        print("Latest file concept not implemented.")
        # Need to know current data path but only the filewriters know this.
        # Auspex may not be able to support the notion of a latest data file
        # without digging into the saved parameters.

    print("Loading file: {}".format(filepath))

    data, descriptors = load_from_HDF5(filepath)
    if showPlot:
        if len(data.keys) == 1: # single qubit data
            for i in data.keys():
                dims = descriptors[i].num_dims()
            if dims == 1: # single dimention data
                #plt.plot()
            if dims == 2: # two dimention data
                #plt.imshow()
        elif len(data.keys) > 1: # multi qubit data
            #pass for now
    return data, descriptors

def get_file_name():
    """Helper function to get a filepath from a dialog box"""

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw() # remove edges

    filepath = filedialog.askopenfilename()
    root.update() # fixes OSX hanging issue
    #https://stackoverflow.com/questions/21866537/what-could-cause-an-open-file-dialog-window-in-tkinter-python-to-be-really-slow

    return filepath

def id_h5file_type(filename):
    """determine .h5 data file version {matlab,legacy,current}"""
    with h5py.File(filename_or_fileobject, 'r') as f:
        # older matlab files with have a path
        pass

if __name__ == '__main__':
    filename = "test_writehdf5_adaptive_unstructured-0000.h5"
    data, desc = load_from_HDF5(filename)
    for a in desc.axes:
        print(a)
    print(data)
    print([k for k in data.dtype.fields.keys()])
