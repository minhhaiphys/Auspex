from __future__ import print_function, division
import time
import logging
logging.basicConfig(format='%(levelname)s:\t%(message)s', level=logging.INFO)

import numpy as np
import scipy as sp
import pandas as pd

from instruments.kepco import BOP2020M
from instruments.stanford import SR865
from sweep import Sweep
from procedure import FloatParameter, Quantity, Procedure


class FieldTest(Procedure):
    current = FloatParameter("Supply Current", unit="A")
    voltage = Quantity("Magnitude", unit="V")

    bop = BOP2020M("Kepco Power Supply", "GPIB1::1::INSTR")
    lock = SR865("Lockin Amplifier", "GPIB1::9::INSTR")

    def instruments_init(self):
        self.tc_delay = 9*self.lock.tc
        self.averages = 10
        self.bop.output = True

        def lockin_measure():
            time.sleep(self.tc_delay)
            return np.mean( [self.lock.r for i in range(self.averages)] )

        self.current.set_method(self.bop.set_current)
        self.voltage.set_method(lockin_measure)

    def run(self):
        """This is run for each step in a sweep."""
        for param in self._parameters:
            self._parameters[param].push()
        for quant in self._quantities:
            self._quantities[quant].measure()

    def instruments_shutdown(self):
        self.bop.current = 0.0

if __name__ == '__main__':

    proc = FieldTest()

    # Define a sweep over prarameters
    sw = Sweep(proc)
    values = np.append(np.arange(-2, 10.1, 0.1), np.arange(10.0,-2.1,-0.1)).tolist()
    sw.add_parameter(proc.current, values)

    # Define a writer
    sw.add_writer('CurrentLoops.h5', 'Test', proc.voltage)

    proc.instruments_init()
    for i in sw:
        logging.info("Current, Lockin Magnitude: {:f}, {:g}".format(proc.current.value, proc.voltage.value) )
    proc.instruments_shutdown()