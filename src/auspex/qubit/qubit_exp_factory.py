# Copyright 2016 Raytheon BBN Technologies
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

import json
import sys
import os
import importlib
import pkgutil
import inspect
import re
import asyncio
import base64
import datetime
import subprocess
import copy
from pony.orm import *

import numpy as np
import networkx as nx

import auspex.config as config
import auspex.instruments
import auspex.filters
import bbndb
from .qubit_exp import QubitExperiment
from auspex.log import logger

def correct_resource_name(resource_name):
    substs = {"USB::": "USB0::", }
    for k, v in substs.items():
        resource_name = resource_name.replace(k, v)
    return resource_name

class QubitExpFactory(object):
    """Create and run Qubit Experiments."""
    def __init__(self, pipeline=None):
        self.pipeline = pipeline

        if bbndb.database:
            self.db = bbndb.database
        else:
            raise Exception("Auspex currently expects db to be loaded already by QGL")
            # config.load_db()
            # if database_file:
            #     self.database_file = database_file
            # elif config.db_file:
            #     self.database_file = config.db_file
            # else:
            #     self.database_file = ":memory:"

            # # self.db = Database()

            # # Define auspex and QGL database entities
            # filter_db.define_entities(self.db)

            # self.db.bind('sqlite', filename=self.database_file, create_db=True)
            # self.db.generate_mapping(create_tables=True)
       
        self.stream_hierarchy = [bbndb.auspex.Demodulate, bbndb.auspex.Integrate, bbndb.auspex.Average, bbndb.auspex.OutputProxy]
        self.filter_map = {
            bbndb.auspex.Demodulate: auspex.filters.Channelizer,       
            bbndb.auspex.Average: auspex.filters.Averager,          
            bbndb.auspex.Integrate: auspex.filters.KernelIntegrator,
            bbndb.auspex.Write: auspex.filters.WriteToHDF5
        }
        self.stream_sel_map = {
            'X6-1000M': auspex.filters.X6StreamSelector,
            'AlazarATS9870': auspex.filters.AlazarStreamSelector
        }
        self.dig_map = {
            'X6-1000M': auspex.instruments.X6,
            'AlazarATS9870': auspex.instruments.AlazarATS9870
        }
        self.source_map = {
            'HolzworthHS9000': auspex.instruments.HolzworthHS9000
        }

    def create(self, meta_file):
        self.meta_file = meta_file
        with open(self.meta_file, 'r') as FID:
            self.meta_info = json.load(FID)

        # Create our experiment instance
        exp = QubitExperiment()

        # Make database bbndb.auspex.connection
        db_provider  = self.meta_info['database_info']['db_provider']
        db_filename  = self.meta_info['database_info']['db_filename']
        library_name = self.meta_info['database_info']['library_name']
        library_id   = self.meta_info['database_info']['library_id']

        # For now we must use the same database
        # if db_filename != self.database_file:
        #     raise Exception("Auspex and QGL must share the same database for now.")

        # Load the channel library by ID
        exp.channelDatabase   = self.channelDatabase  = bbndb.qgl.ChannelDatabase[library_id]
        exp.all_channels      = self.all_channels     = list(self.channelDatabase.channels)
        exp.all_sources       = self.all_sources      = list(self.channelDatabase.sources)
        exp.all_awgs          = self.all_awgs         = list(self.channelDatabase.awgs)
        exp.all_digitizers    = self.all_digitizers   = list(self.channelDatabase.digitizers)
        exp.all_qubits        = self.all_qubits       = [c for c in self.all_channels if isinstance(c, bbndb.qgl.Qubit)]
        exp.all_measurements  = self.all_measurements = [c for c in self.all_channels if isinstance(c, bbndb.qgl.Measurement)]

        # Restrict to current qubits, channels, etc. involved in this actual experiment
        # Based on the meta info
        exp.controlled_qubits = self.controlled_qubits = list(self.channelDatabase.channels.filter(lambda x: x.label in self.meta_info["qubits"]))
        exp.measurements      = self.measurements      = list(self.channelDatabase.channels.filter(lambda x: x.label in self.meta_info["measurements"]))
        exp.measured_qubits   = self.measured_qubits   = list(self.channelDatabase.channels.filter(lambda x: "M-"+x.label in self.meta_info["measurements"]))
        exp.phys_chans        = self.phys_chans        = list(set([e.phys_chan for e in self.controlled_qubits + self.measurements]))
        exp.awgs              = self.awgs              = list(set([e.phys_chan.awg for e in self.controlled_qubits + self.measurements]))
        exp.receivers         = self.receivers         = list(set([e.receiver_chan for e in self.measurements]))
        exp.digitizers        = self.digitizers        = list(set([e.receiver_chan.digitizer for e in self.measurements]))
        exp.sources           = self.sources           = [q.phys_chan.generator.label for q in self.measured_qubits + self.controlled_qubits + self.measurements if q.phys_chan.generator]

        # Add the waveform file info to the qubits
        for awg in self.awgs:
            awg.sequence_file = self.meta_info['instruments'][awg.label]

        # Construct the DataAxis from the meta_info
        desc = self.meta_info["axis_descriptor"]
        data_axis = desc[0] # Data will always be the first axis

        # ovverride data axis with repeated number of segments
        if hasattr(exp, "repeats") and exp.repeats is not None:
            data_axis['points'] = np.tile(data_axis['points'], exp.repeats)

        # Search for calibration axis, i.e., metadata
        axis_names = [d['name'] for d in desc]
        if 'calibration' in axis_names:
            meta_axis = desc[axis_names.index('calibration')]
            # There should be metadata for each cal describing what it is
            if len(desc)>1:
                metadata = ['data']*len(data_axis['points']) + meta_axis['points']
                # Pad the data axis with dummy equidistant x-points for the extra calibration points
                avg_step = (data_axis['points'][-1] - data_axis['points'][0])/(len(data_axis['points'])-1)
                points = np.append(data_axis['points'], data_axis['points'][-1] + (np.arange(len(meta_axis['points']))+1)*avg_step)
            else:
                metadata = meta_axis['points'] # data may consist of calibration points only
                points = np.arange(len(metadata)) # dummy axis for plotting purposes
            # If there's only one segment we can ignore this axis
            if len(points) > 1:
                exp.segment_axis = DataAxis(data_axis['name'], points, unit=data_axis['unit'], metadata=metadata)
        else:
            # No calibration data, just add a segment axis as long as there is more than one segment
            if len(data_axis['points']) > 1:
                exp.segment_axis = DataAxis(data_axis['name'], data_axis['points'], unit=data_axis['unit'])

        # Build graphs
        self.meas_graph = nx.DiGraph()

        # Build a mapping of qubits to receivers, construct qubit proxies
        self.receivers_by_qubit = {self.channelDatabase.channels.filter(lambda x: x.label == e.label[2:]).first(): e.receiver_chan for e in self.measurements}
        self.qubit_proxies = {q: bbndb.auspex.QubitProxy(self, q.label) for q in self.measured_qubits + self.controlled_qubits}
        self.stream_info_by_qubit = {q.label: r.digitizer.stream_types for q,r in self.receivers_by_qubit.items()}

        for q, r in self.receivers_by_qubit.items():
            qp = self.qubit_proxies[q]
            qp.available_streams = [st.strip() for st in r.digitizer.stream_types.split(",")]
            qp.stream_type = qp.available_streams[-1]
        
        # If no pipeline is defined, assumed we want to generate it automatically
        if self.pipeline is None:
            for q in self.receivers_by_qubit.keys():
                self.qubit_proxies[q].auto_create_pipeline()
            commit()

        # Now a pipeline exists, so we create Auspex filters from the proxy filters in the db
        proxy_to_filter = {}
        connector_by_qp = {}

        # Create the digitizer instruments from the database objects.
        # We configure the instrument later after adding channels
        for dig in self.digitizers:
            dig.instr = self.dig_map[dig.model]() # For easy lookup

        for mq in self.measured_qubits:

            # Create the stream selectors
            rcv = self.receivers_by_qubit[mq]
            dig = rcv.digitizer
            stream_sel = self.stream_sel_map[dig.model](name=rcv.label+'-SS')
            stream_sel.configure_with_proxy(rcv)
            stream_sel.receiver = stream_sel.proxy = rcv

            # Construct the channel from the receiver
            channel = stream_sel.get_channel(rcv)

            # Get the base descriptor from the channel
            descriptor = stream_sel.get_descriptor(rcv)

            # Update the descriptor based on the number of segments
            # The segment axis should already be defined if the sequence
            # is greater than length 1
            if hasattr(exp, "segment_axis"):
                descriptor.add_axis(exp.segment_axis)

            # Add the channel to the instrument
            dig.instr.add_channel(channel)

            # Add the output connectors to the experiment and set their base descriptor
            mqp = self.qubit_proxies[mq]
            connector_by_qp[mqp] = exp.add_connector(mqp)
            connector_by_qp[mqp].set_descriptor(descriptor)          

        # Configure digitizer instruments from the database objects
        # this must be done after adding channels.
        for dig in self.digitizers:
            dig.instr.configure_with_proxy(dig)

        # Configure the individual filter nodes
        for node in self.meas_graph.nodes():
            if isinstance(node, bbndb.auspex.FilterProxy):
                new_filt = self.filter_map[type(node)]()
                new_filt.configure_with_proxy(node)
                new_filt.proxy = node
                proxy_to_filter[node] = new_filt
        
        # Connect the filters together
        graph_edges = []
        for node1, node2 in self.meas_graph.edges():
            if isinstance(node1, bbndb.auspex.FilterProxy):
                filt1 = proxy_to_filter[node1]
                oc   = filt1.output_connectors["source"]
            elif isinstance(node1, bbndb.auspex.QubitProxy):
                oc   = connector_by_qp[node1]
            filt2 = proxy_to_filter[node2]
            ic   = filt2.input_connectors["sink"]
            graph_edges.append([oc, ic])

        # Define the experiment graph
        exp.set_graph(graph_edges)

        return exp

    def qubit(self, qubit_name):
        return {q.label: qp for q,qp in self.qubit_proxies.items()}[qubit_name]

    def save_pipeline(self, name):
        cs = [bbndb.auspex.Connection(pipeline_name=name, node1=n1, node2=n2) for n1, n2 in self.meas_graph.edges()]
    
    def load_pipeline(self, pipeline_name):
        cs = select(c for c in bbndb.auspex.Connection if c.pipeline_name==pipeline_name)
        if len(cs) == 0:
            print(f"No results for pipeline {pipeline_name}")
            return
        else:
            temp_edges = [(c.node1, c.node2) for c in cs]
            self.meas_graph.clear()
            self.meas_graph.add_edges_from(temp_edges)
    
    def show_pipeline(self, pipeline_name=None):
        """If a pipeline name is specified query the database, otherwise show the 
        current pipeline."""
        if pipeline_name:
            cs = select(c for c in bbndb.auspex.Connection if c.pipeline_name==pipeline_name)
            if len(cs) == 0:
                print(f"No results for pipeline {pipeline_name}")
                return
            temp_edges = [(c.node1, c.node2) for c in cs]
            graph = nx.DiGraph()
            graph.add_edges_from(temp_edges)
        else:
            graph = self.meas_graph
            
        labels = {n: n.label() for n in graph.nodes()}
        colors = ["#3182bd" if isinstance(n, bbndb.auspex.QubitProxy) else "#ff9933" for n in graph.nodes()]
        plot_graph(graph, labels, colors=colors)
    
    def reset_pipelines(self):
        for qp in self.qubit_proxies.values():
            qp.clear_pipeline()
            qp.auto_create_pipeline()
    
    def show_connectivity(self):
        pass

def plot_graph(graph, labels, prog="dot", colors='r'):
    import matplotlib.pyplot as plt

    pos = nx.drawing.nx_pydot.graphviz_layout(graph, prog=prog)
    
    # Create position copies for shadows, and shift shadows
    pos_shadow = copy.copy(pos)
    pos_labels = copy.copy(pos)
    for idx in pos_shadow.keys():
        pos_shadow[idx] = (pos_shadow[idx][0] + 0.01, pos_shadow[idx][1] - 0.01)
        pos_labels[idx] = (pos_labels[idx][0] + 0, pos_labels[idx][1] + 20 )
    nx.draw_networkx_nodes(graph, pos_shadow, node_size=100, node_color='k', alpha=0.5)
    nx.draw_networkx_nodes(graph, pos, node_size=100, node_color=colors, linewidths=1, alpha=1.0)
    nx.draw_networkx_edges(graph, pos, width=1)
    nx.draw_networkx_labels(graph, pos_labels, labels)
    
    ax = plt.gca()
    ax.axis('off')
    ax.set_xlim((ax.get_xlim()[0]-20.0, ax.get_xlim()[1]+20.0))
    ax.set_ylim((ax.get_ylim()[0]-20.0, ax.get_ylim()[1]+20.0))
    plt.show()

