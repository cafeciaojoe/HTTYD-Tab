import logging
import sys

import cflib.crtp
from cflib.crazyflie import Crazyflie
from cflib.crazyflie.log import LogConfig
from cflib.crazyflie.mem import MemoryElement

from cfclient.utils.ui import UiUtils

from PyQt5 import QtWidgets
from PyQt5 import uic
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtCore import pyqtSlot
from PyQt5.QtCore import QDir
from PyQt5.QtCore import QThread
from PyQt5.QtCore import QUrl
from PyQt5.QtWidgets import QAction
from PyQt5.QtWidgets import QActionGroup
from PyQt5.QtWidgets import QShortcut
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QLabel
from PyQt5.QtWidgets import QMenu
from PyQt5.QtWidgets import QMessageBox
from PyQt5 import QtWidgets
from PyQt5 import uic

logging.basicConfig(level=logging.INFO)


class MyDockWidget(QtWidgets.QDockWidget):
    closed = pyqtSignal()

    def closeEvent(self, event):
        super(MyDockWidget, self).closeEvent(event)
        self.closed.emit()

class UIState:
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2


class BatteryStates:
    BATTERY, CHARGING, CHARGED, LOW_POWER = list(range(4))


class HTTYD(QtWidgets.QMainWindow):
    connectionLostSignal = pyqtSignal(str, str)
    connectionInitiatedSignal = pyqtSignal(str)
    batteryUpdatedSignal = pyqtSignal(int, object, object)
    connectionDoneSignal = pyqtSignal(str)
    connectionFailedSignal = pyqtSignal(str, str)
    disconnectedSignal = pyqtSignal(str)
    linkQualitySignal = pyqtSignal(int)

    _log_error_signal = pyqtSignal(object, str)

    def __init__(self):
        super(HTTYD, self).__init__()
        uic.loadUi('HTTYD.ui', self)
        self.show()

        self.cf = Crazyflie(rw_cache='./cache')

        cflib.crtp.init_drivers(enable_debug_driver=False)

        self.cf.connection_failed.add_callback(
            self.connectionFailedSignal.emit)
        self.connectionFailedSignal.connect(self._connection_failed)

        # Connect UI signals
        self.connectButton.clicked.connect(self._connect)
        self.batteryUpdatedSignal.connect(self._update_battery)

        self.address.setValue(0xE7E7E7E7E7)

        self._disable_input = False

        # Connection callbacks and signal wrappers for UI protection
        self.cf.connected.add_callback(self.connectionDoneSignal.emit)
        self.connectionDoneSignal.connect(self._connected)
        self.cf.disconnected.add_callback(self.disconnectedSignal.emit)
        self.disconnectedSignal.connect(self._disconnected)
        self.cf.connection_lost.add_callback(self.connectionLostSignal.emit)
        self.connectionLostSignal.connect(self._connection_lost)
        self.cf.connection_requested.add_callback(
            self.connectionInitiatedSignal.emit)
        self.connectionInitiatedSignal.connect(self._connection_initiated)
        self._log_error_signal.connect(self._logging_error)

        self.batteryBar.setTextVisible(False)
        self.linkQualityBar.setTextVisible(False)

        # Connect link quality feedback
        self.cf.link_quality_updated.add_callback(self.linkQualitySignal.emit)
        self.linkQualitySignal.connect(
            lambda percentage: self.linkQualityBar.setValue(percentage))

        self.uiState = UIState.DISCONNECTED
        self._update_ui_state()

    def _update_ui_state(self):
        if self.uiState == UIState.DISCONNECTED:
            self.setWindowTitle("Not connected")
            self.connectButton.setText("Connect")
            self.connectButton.setEnabled(True)
            self.scanButton.setEnabled(True)
            self.address.setEnabled(True)
            self.batteryBar.setValue(3000)
            self.linkQualityBar.setValue(0)
        elif self.uiState == UIState.CONNECTED:
            self.setWindowTitle("Connected")
            self.connectButton.setText("Disconnect")
        elif self.uiState == UIState.CONNECTING:
            self.setWindowTitle("Connecting")
            self.connectButton.setText("Cancel")
            self.address.setEnabled(False)

    def _update_battery(self, timestamp, data, logconf):
        self.batteryBar.setValue(int(data["pm.vbat"] * 1000))

        color = UiUtils.COLOR_BLUE
        # TODO firmware reports fully-charged state as 'Battery',
        # rather than 'Charged'
        if data["pm.state"] in [BatteryStates.CHARGING, BatteryStates.CHARGED]:
            color = UiUtils.COLOR_GREEN
        elif data["pm.state"] == BatteryStates.LOW_POWER:
            color = UiUtils.COLOR_RED

        self.batteryBar.setStyleSheet(UiUtils.progressbar_stylesheet(color))
        self._aff_volts.setText(("%.3f" % data["pm.vbat"]))

    def _connected(self):
        self.uiState = UIState.CONNECTED
        self._update_ui_state()

        # todo link uri value delt with?
        # Config().set("link_uri", str(self._selected_interface))

        lg = LogConfig("Battery", 1000)
        lg.add_variable("pm.vbat", "float")
        lg.add_variable("pm.state", "int8_t")
        try:
            self.cf.log.add_config(lg)
            lg.data_received_cb.add_callback(self.batteryUpdatedSignal.emit)
            lg.error_cb.add_callback(self._log_error_signal.emit)
            lg.start()
        except KeyError as e:
            logging.warning(str(e))

        mems = self.cf.mem.get_mems(MemoryElement.TYPE_DRIVER_LED)
        if len(mems) > 0:
            mems[0].write_data(self._led_write_done)

    def _disconnected(self):
        self.uiState = UIState.DISCONNECTED
        self._update_ui_state()

    def _connection_initiated(self):
        self.uiState = UIState.CONNECTING
        self._update_ui_state()

    def _led_write_done(self, mem, addr):
        logging.info("LED write done callback")

    def _logging_error(self, log_conf, msg):
        QMessageBox.about(self, "Log error", "Error when starting log config"
                                             " [{}]: {}".format(log_conf.name,
                                                                msg))

    def _connection_lost(self, linkURI, msg):
        warningCaption = "Communication failure"
        error = "Connection lost to {}: {}".format(linkURI, msg)
        QMessageBox.critical(self, warningCaption, error)
        self.uiState = UIState.DISCONNECTED
        self._update_ui_state()

    def _connection_failed(self, linkURI, error):
        msg = "Failed to connect on {}: {}".format(linkURI, error)
        warningCaption = "Communication failure"
        QMessageBox.critical(self, warningCaption, msg)
        self.uiState = UIState.DISCONNECTED
        self._update_ui_state()

    def closeEvent(self, event):
        self.hide()
        self.cf.close_link()


if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    window = HTTYD()
    sys.exit(app.exec_())