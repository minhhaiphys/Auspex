# Copyright 2016 Raytheon BBN Technologies
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

import auspex.config as config
from copy import copy
import os
import json

import numpy as np
from scipy.optimize import curve_fit
import time

from auspex.log import logger
from auspex.experiment import FloatParameter, IntParameter, Experiment
from auspex.stream import DataStream, DataAxis, DataStreamDescriptor, OutputConnector

from JSONLibraryUtils import LibraryCoders

def find_null_offset(xpts, powers):
    """Finds the offset corresponding to the minimum power using a fit to the measured data"""
    def model(x, a, b, c):
        return 10*np.log10(a*(x - b)**2 + c)
    min_idx = np.argmin(powers)
    fit = curve_fit(xpts, powers, p0=[1, xpts[min_idx], pow(10, powers[min_idx]/10)])
    best_offset = np.real(fit[1])
    best_offset = np.minimum(best_offset, xpts[-1])
    best_offset = np.maximum(best_offset, xpts[0])
    fit_pts = np.array([np.real(model(x, *fit)) for x in xpts])
    return best_offset, fit_pts

class MixerCalibrationExperiment(Experiment):

    SSB_FREQ = 100000000

    amplitude = OutputConnector(unit='dBc')

    I_offset = FloatParameter(default=0.0, unit="V")
    Q_offset = FloatParameter(default=0.0, unit="V")
    amplitude_factor = FloatParameter(default=1.0)
    phase_skew = FloatParameter(default=0.0, unit="deg")


    def __init__(self, qubit, mixer="control"):
        """Initialize MixerCalibrationExperiment Experiment.
            Args:
                qubit: Qubit identifier string.
                mixer: One of 'control', 'measure' to select which mixer to cal.
        """
        if mixer not in ("measure", "control"):
            raise ValueError("Unknown mixer {}: must be either 'measure' or 'control'.".format(mixer))
            self.mixer = mixer
        self.settings = config.yaml_load(config.configFile)
        sa = [name for name, settings in self.settings['instruments'].items() if settings['type'] == 'SpectrumAnalyzer']
        if len(sa) > 1:
            raise ValueError("More than one spectrum analyzer is defined in the configuration file.")
        if len(sa) == 0:
                raise ValueError("No spectrum analyzer is defined in the configuration file.")
        self.sa = sa[0]
        logger.debug("Found spectrum analyzer: {}.".format(self.sa))
        if "LO" not in self.settings['instruments'][self.sa].keys():
            raise ValueError("No local oscillator is defined for spectrum analyzer {}.".format(self.sa))
        try:
            self.LO = self.settings['instruments'][self.sa]['LO']
        except KeyError:
            raise ValueError("LO {} for spectrum analyzer {} not found in instrument configuration file!".format(self.LO, self.sa))
        try:
            self.qubit = qubit
            self.qubit_settings = self.settings['qubits'][qubit]
        except KeyError as ex:
            raise ValueError("Could not find qubit {} in the qubit configuration file.".format(qubit)) from ex
        self.AWG = self.settings['qubits'][qubit][mixer]['AWG'].split(" ")[0]
        if self.settings['instruments'][self.AWG]['type'] != 'APS2':
            raise ValueError("Mixer calibration only supported for APS2.")
        self.source = self.settings['qubits'][qubit][mixer]['generator']
        self.instruments_to_enable = [self.sa, self.LO, self.AWG, self.source]
        self.instrs_connected = False
        super(MixerCalibrationExperiment, self).__init__()

    def init_instruments(self):

        self.I_offset.assign_method(lambda x: self._instruments[self.AWG].set_offset(0, x))
        self.Q_offset.assign_method(lambda x: self._instruments[self.AWG].set_offset(1, x))
        self.amplitude_factor.assign_method(self._instruments[self.AWG].set_mixer_amplitude_imbalance)
        self.phase_skew.assign_method(self._instruments[self.AWG].set_mixer_phase_skew)

        self.I_offset.add_post_push_hook(lambda: time.sleep(0.01))
        self.Q_offset.add_post_push_hook(lambda: time.sleep(0.01))
        self.amplitude_factor.add_post_push_hook(lambda: time.sleep(0.01))
        self.phase_skew.add_post_push_hook(lambda: time.sleep(0.01))

        for name, instr in self._instruments.items():
            instr_par = self.settings['instruments'][name]
            if instr_par['type'] == 'APS2':
                instr_par['seq_file'] = None
            logger.debug("Setting instr %s with params %s.", name, instr_par)
            instr.set_all(instr_par)

        #make sure the microwave generators are set up properly
        self._instruments[self.source].output = True
        self._instruments[self.LO].frequency = self._instruments[self.source].frequency - self._instruments[self.sa].IF_FREQ
        self._instruments[self.LO].output = True
        self._setup_awg_ssb()

    def _setup_awg_ssb(self):
        #set up ingle sideband modulation IQ playback on the AWG
        self._instruments[self.AWG].stop()
        self._instruments[self.AWG].load_waveform(1, 0.5*np.ones(1200, dtype=np.float))
        self._instruments[self.AWG].load_waveform(2, np.zeros(1200, dtype=np.float))
        self._instruments[self.AWG].waveform_frequency = -self.SSB_FREQ
        self._instruments[self.AWG].run_mode = "CW_WAVEFORM"
        #rest mixer correction and I/Q offsets
        self._instruments[self.AWG].set_mixer_amplitude_imbalance(1.0)
        self._instruments[self.AWG].set_mixer_phase_skew(0.0)
        #start playback
        self._instruments[self.AWG].run()
        logger.debug("Playing SSB CW IQ modulation on {} at frequency: {} MHz".format(self.AWG, self.SSB_FREQ/1e6))

    def shutdown_instruments(self):
        #reset the APS2, just in case.
        self._instruments[self.LO].output = False
        self._instruments[self.source].output = False
        self._instruments[self.AWG].stop()
        for name, instr in self._instruments.items():
            instr.disconnect()

    def init_streams(self):
        pass

    async def run(self):
        await self.amplitude.push(self._instruments[self.sa].peak_amplitude())
