#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#     ||          ____  _ __
#  +------+      / __ )(_) /_______________ _____  ___
#  | 0xBC |     / __  / / __/ ___/ ___/ __ `/_  / / _ \
#  +------+    / /_/ / / /_/ /__/ /  / /_/ / / /_/  __/
#   ||  ||    /_____/_/\__/\___/_/   \__,_/ /___/\___/
#
#  Copyright (C) 2011-2013 Bitcraze AB
#
#  Crazyflie Nano Quadcopter Client
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.

#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
#  02110-1301, USA.

import logging
import math
import time
from enum import Enum

from PyQt5 import uic
from PyQt5.QtCore import pyqtSignal, pyqtSlot, QObject, pyqtProperty
from PyQt5.QtCore import QStateMachine, QState, QEvent, QTimer
from PyQt5.QtCore import QAbstractTransition
from PyQt5.QtWidgets import QMessageBox

import cfclient
from cfclient.ui.tab import Tab
from cflib.crazyflie.log import LogConfig
from cflib.crazyflie.syncLogger import SyncLogger
from cflib.crazyflie.syncCrazyflie import SyncCrazyflie
from cflib.crazyflie import Crazyflie

import threading

__author__ = 'Bitcraze AB'
__all__ = ['HTTYD']

logger = logging.getLogger(__name__)

HTTYD_tab_class = uic.loadUiType(cfclient.module_path +
                                   "/ui/tabs/HTTYD.ui")[0]

class FlightModeEvent(QEvent):

    def __init__(self, mode, parent=None):
        super(FlightModeEvent, self).__init__(QEvent.Type(QEvent.User + 1))
        self.mode = mode

class FlightModeTransition(QAbstractTransition):

    def __init__(self, value, parent=None):
        super(FlightModeTransition, self).__init__(parent)
        self.value = value

    def eventTest(self, event):
        if event.type() != QEvent.Type(QEvent.User + 1):
            return False

        return event.mode == self.value

    def onTransition(self, event):
        pass

class FlightModeStates(Enum):
    LAND = 0
    LIFT = 1
    FOLLOW = 2
    PATH = 3
    HOVERING = 4
    GROUNDED = 5
    DISCONNECTED = 6
    CIRCLE = 7
    RECORD = 8

class HTTYD(Tab, HTTYD_tab_class):
    """Define some signals that will emit some string,
    signals a are usually sent by buttons
    these signals need to be connected to a slot/slots. 
    so for example, when the CF is connected, a bunch of things
    in the GUI happen. 
    
    https://youtu.be/GIg9ehmGJHY?t=1420
    """
    _connected_signal = pyqtSignal(str)
    _disconnected_signal = pyqtSignal(str)
    _log_data_signal = pyqtSignal(int, object, object)
    _log_error_signal = pyqtSignal(object, str)
    _param_updated_signal = pyqtSignal(str, str)

    cfStatusChanged = pyqtSignal(str)
    statusChanged = pyqtSignal(str)

    def __init__(self, tabWidget, helper, *args):
        super(HTTYD, self).__init__(*args)
        self.setupUi(self)

        self._machine = QStateMachine()
        self._setup_states()
        self._event = threading.Event()

        self.tabName = "HTTYD"
        self.menuName = "HTTYD Tab"
        self.tabWidget = tabWidget

        # the above helper cf instances are only assigned to  _cf_L and _cf_R after they start logging
        self._helper = helper
        self._cf = None

        self.last_time = 0

        # assign the label to the _cf_status_ string
        self._cf_status = self.cfStatusLabel.text()
        self._status = self.statusLabel.text()

        # initial flight mode
        self.flying_enabled = False
        self.switch_flight_mode(FlightModeStates.DISCONNECTED)
        self.path_pos_threshold = 0.2

        # The position and rotation of the cf and wand obtained by the
        # lighthouse tracking, if it cant be tracked the position becomes Nan
        self.cf_pos = Position(0, 0, 0)
        self.wand_pos = Position(0, 0, 0)

        # The regular cf_pos can a times due to lost tracing become Nan,
        # this the latest known valid cf position
        self.valid_cf_pos = Position(0, 0, 0)

        # Always wrap callbacks from Crazyflie API though QT Signal/Slots
        # to avoid manipulating the UI when rendering it
        self._connected_signal.connect(self._connected)
        self._disconnected_signal.connect(self._disconnected)
        self._log_data_signal.connect(self._log_data_received)
        self._param_updated_signal.connect(self._param_updated)

        # connect the status change signal to the update status
        # funciton
        self.statusChanged.connect(self._update_status)
        self.cfStatusChanged.connect(self._update_cf_status)

        # Connect the Crazyflie API callbacks to the signals
        self._helper.cf.connected.add_callback(self._connected_signal.emit)

        self._helper.cf.disconnected.add_callback(self._disconnected_signal.emit)

        # Connect the UI elements
        self.liftButton.clicked.connect(self.set_lift_mode)
        self.landButton.clicked.connect(self.set_land_mode)
        self.followButton.clicked.connect(self.set_follow_mode)
        self.emergencyButton.clicked.connect(self.set_kill_engine)

    def _setup_states(self):
        parent_state = QState()

        # DISCONNECTED
        disconnected = QState(parent_state)
        disconnected.assignProperty(self, "status", "Disabled")
        disconnected.assignProperty(self.followButton, "text", "Follow Mode")
        disconnected.assignProperty(self.emergencyButton, "enabled", False)
        disconnected.assignProperty(self.liftButton, "enabled", False)
        disconnected.assignProperty(self.followButton, "enabled", False)
        disconnected.assignProperty(self.landButton, "enabled", False)
        disconnected.entered.connect(self._flight_mode_disconnected_entered)

        # HOVERING
        hovering = QState(parent_state)
        hovering.assignProperty(self, "status", "Hovering...")
        hovering.assignProperty(self.followButton, "text", "Follow Mode")
        hovering.assignProperty(self.emergencyButton, "enabled", True)
        hovering.assignProperty(self.liftButton, "enabled", False)
        hovering.assignProperty(self.followButton, "enabled", True)
        hovering.assignProperty(self.landButton, "enabled", True)
        hovering.entered.connect(self._flight_mode_hovering_entered)

        # GROUNDED
        grounded = QState(parent_state)
        grounded.assignProperty(self, "status", "Grounded")
        grounded.assignProperty(self.followButton, "text", "Follow Mode")
        grounded.assignProperty(self.emergencyButton, "enabled", True)
        grounded.assignProperty(self.liftButton, "enabled", True)
        grounded.assignProperty(self.followButton, "enabled", False)
        grounded.assignProperty(self.landButton, "enabled", False)
        grounded.entered.connect(self._flight_mode_grounded_entered)

        # FOLLOW
        follow = QState(parent_state)
        follow.assignProperty(self, "status", "Follow Mode")
        follow.assignProperty(self.followButton, "text", "Stop")
        follow.assignProperty(self.emergencyButton, "enabled", True)
        follow.assignProperty(self.landButton, "enabled", True)
        follow.assignProperty(self.followButton, "enabled", False)
        follow.assignProperty(self.liftButton, "enabled", False)
        follow.assignProperty(self.recordButton, "enabled", False)
        follow.entered.connect(self._flight_mode_follow_entered)

        # LIFT
        lift = QState(parent_state)
        lift.assignProperty(self, "status", "Lifting...")
        lift.assignProperty(self.emergencyButton, "enabled", True)
        lift.assignProperty(self.liftButton, "enabled", False)
        lift.assignProperty(self.followButton, "enabled", False)
        lift.assignProperty(self.landButton, "enabled", True)
        lift.entered.connect(self._flight_mode_lift_entered)

        # LAND
        land = QState(parent_state)
        land.assignProperty(self, "status", "Landing...")
        land.assignProperty(self.emergencyButton, "enabled", True)
        land.assignProperty(self.liftButton, "enabled", False)
        land.assignProperty(self.followButton, "enabled", False)
        land.assignProperty(self.landButton, "enabled", False)
        land.entered.connect(self._flight_mode_land_entered)

        def add_transition(mode, child_state, parent):
            transition = FlightModeTransition(mode)
            transition.setTargetState(child_state)
            parent.addTransition(transition)

        add_transition(FlightModeStates.LAND, land, parent_state)
        add_transition(FlightModeStates.LIFT, lift, parent_state)
        add_transition(FlightModeStates.HOVERING, hovering, parent_state)
        add_transition(FlightModeStates.FOLLOW, follow, parent_state)
        add_transition(FlightModeStates.GROUNDED, grounded, parent_state)
        add_transition(FlightModeStates.DISCONNECTED, disconnected,
                       parent_state)

        parent_state.setInitialState(disconnected)
        self._machine.addState(parent_state)
        self._machine.setInitialState(parent_state)
        self._machine.start()

    """
    update flight status is called when;
    - the CF is connected or disconnected
    - the QTM (or in our case the poseNet) is connected or disconnected
    it ensure that they are both connected before starting the flight controller. 
    """
    def _update_flight_status(self):
        """
        assign old state to new state
        """
        prev_flying_enabled = self.flying_enabled
        """
        if there is a cf instance and a qtm connection instance
        (even if they are not connected)
        then flying is enabled
        """
        self.flying_enabled = (self._cf is not None)
                              # and \
            # self._qtm_connection is not None

        """
        if the flying enabled is not the same as prev_flying enabled" 
        an additional check for security...?
        """
        if not prev_flying_enabled and self.flying_enabled:
            self.switch_flight_mode(FlightModeStates.GROUNDED)
            t1 = threading.Thread(target=self.flight_controller)
            # t2 = threading.Thread(target=self.flight_logger)

            t1.start()
            # t2.start()

        """
        if either the CF or QTM/Posenet Drops out. 
        flight mode is disconnect
        """
        if prev_flying_enabled and not self.flying_enabled:
            self.switch_flight_mode(FlightModeStates.DISCONNECTED)

    """
    Although PyQt allows any Python callable to be used as a slot when 
    connecting signals, it is sometimes necessary to explicitly mark a 
    Python method as being a Qt slot and to provide a C++ signature for it. 
    PyQt4 provides the pyqtSlot() function decorator to do this
    """
    @pyqtSlot(str)
    def _update_cf_status(self, status):
        self.cfStatusLabel.setText(status)

    @pyqtSlot(str)
    def _update_status(self, status):
        self.statusLabel.setText("Status: {}".format(status))


    """
    A new Qt property may be defined using the pyqtProperty function. 
    It is used in the same way as the standard Python property() function. 
    In fact, Qt properties defined in this way also behave as Python properties.
    https://www.riverbankcomputing.com/static/Docs/PyQt5/qt_properties.html
    https://www.youtube.com/watch?v=jCzT9XFZ5bw
    """
    @pyqtProperty(str, notify=cfStatusChanged)
    def cfStatus(self):
        return

    @cfStatus.setter
    def cfStatus(self, value):
        if value != self._cf_status:
            self._cf_status = value
            self.cfStatusChanged.emit(value)

    @pyqtProperty(str, notify=statusChanged)
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        if value != self._status:
            self._status = value
            self.statusChanged.emit(value)

    def _connected(self, link_uri):
        """Callback when the Crazyflie has been connected"""

        self._cf = self._helper.cf
        self._update_flight_status()

        logger.debug("Crazyflie connected to {}".format(link_uri))

        # Gui
        self.cfStatus = ': connected'

        self.t2 = threading.Thread(target=self.flight_logger)
        self.t2.start()

    def _disconnected(self, link_uri):
        """Callback for when the Crazyflie has been disconnected"""

        logger.info("Crazyflie disconnected from {}".format(link_uri))
        self.cfStatus = ': not connected'
        self._cf = None
        self._update_flight_status()

    def _param_updated(self, name, value):
        """Callback when the registered parameter get's updated"""

        logger.debug("Updated {0} to {1}".format(name, value))

    def _log_data_received(self, timestamp, data, log_conf):
        """Callback when the log layer receives new data"""

        logger.debug("{0}:{1}:{2}".format(timestamp, log_conf.name, data))

    def _logging_error(self, log_conf, msg):
        """Callback from the log layer when an error occurs"""

        QMessageBox.about(self, "Example error",
                          "Error when using log config"
                          " [{0}]: {1}".format(log_conf.name, msg))

    def _flight_mode_land_entered(self):
        self.current_goal_pos = self.valid_cf_pos
        logger.info('Trying to land at: x: {} y: {}'.format(
            self.current_goal_pos.x, self.current_goal_pos.y))
        self.land_rate = 1
        print('flight_mode_land_entered')
        self._event.set()

    def _flight_mode_follow_entered(self):
        # self.last_valid_wand_pos = Position(0, 0, 1)
        self._event.set()

    def _flight_mode_lift_entered(self):
        self.current_goal_pos = self.valid_cf_pos
        logger.info('Trying to lift at: {}'.format(
            self.current_goal_pos))
        self._event.set()

    def _flight_mode_hovering_entered(self):
        self.current_goal_pos = self.valid_cf_pos
        logger.info('Hovering at: {}'.format(
            self.current_goal_pos))
        self._event.set()

    def _flight_mode_grounded_entered(self):
        self._event.set()
        print('flight_mode_grounded_entered')

    def _flight_mode_disconnected_entered(self):
        self._event.set()
        print('flight_mode_disconnected_entered')

    def flight_logger(self):
        logger.info('Starting flight logger thread')

        log_angle = LogConfig(name='lighthouse', period_in_ms=100)
        log_angle.add_variable('lighthouse.rawAngle0x', 'float')
        log_angle.add_variable('lighthouse.rawAngle0y', 'float')
        log_angle.add_variable('lighthouse.rawAngle1x', 'float')
        log_angle.add_variable('lighthouse.rawAngle1y', 'float')

        log_position = LogConfig(name='Position', period_in_ms=100)
        log_position.add_variable('stateEstimate.x', 'float')
        log_position.add_variable('stateEstimate.y', 'float')
        log_position.add_variable('stateEstimate.z', 'float')

        rawAngle0x = [0, 0]
        rawAngle0y = [0, 0]
        rawAngle1x = [0, 0]
        rawAngle1y = [0, 0]

        state_estimate = [0, 0, 0]

        with SyncLogger(self._cf, [log_angle,log_position]) as log:
            for log_entry in log:
                if 'lighthouse.rawAngle0x' in log_entry[1]:
                    data_1 = log_entry[1]
                    rawAngle0x.append(data_1['lighthouse.rawAngle0x'])
                    rawAngle0x.pop(0)
                    # rawAngle0y.append(data_1['lighthouse.rawAngle0y'])
                    # rawAngle0y.pop(0)
                    rawAngle1x.append(data_1['lighthouse.rawAngle1x'])
                    rawAngle1x.pop(0)
                    # rawAngle1y.append(data_1['lighthouse.rawAngle1y'])
                    # rawAngle1y.pop(0)

                    # if rawAngle0x[0] == rawAngle0x[1] and rawAngle0y[0] == rawAngle0y[1] and rawAngle1x[0] == \
                    #         rawAngle1x[1] and rawAngle1y[0] == rawAngle1y[1]:
                    print('0x-1x =',rawAngle0x[0]-rawAngle0x[1], '0y-1y =',rawAngle0y[0]-rawAngle1y[1])
                    if rawAngle0x[0] == rawAngle0x[1] and rawAngle1x[0] == rawAngle1x[1]:
                        self.cf_pos = Position(float('nan'), float('nan'), float('nan'))
                        print('setting cf_pos.x to {}'.format(self.cf_pos.x))
                        # print(self.cf_pos.x, self.cf_pos.y, self.cf_pos.z)

                if 'stateEstimate.x' in log_entry[1]:
                    # if rawAngle0x[0] != rawAngle0x[1] and rawAngle0y[0] != rawAngle0y[1] and rawAngle1x[0] != \
                    #         rawAngle1x[1] and rawAngle1y[0] != rawAngle1y[1]:
                    if rawAngle0x[0] != rawAngle0x[1] or rawAngle1x[0] != rawAngle1x[1]:
                        data_2 = log_entry[1]
                        state_estimate[0] = data_2['stateEstimate.x']
                        state_estimate[1] = data_2['stateEstimate.y']
                        state_estimate[2] = data_2['stateEstimate.z']
                        self.cf_pos = Position(state_estimate[0], state_estimate[1], state_estimate[2])
                        print('updating state estimate to {}'.format(self.cf_pos.x))
                # else:
                #     print('unknown log_entry {}'.format(log_entry[1]))
                #     raise Exception

        # except Exception as err:
        #     logger.error(err)
        #     self.cfStatus = str(err)
        #
        # logger.info('Terminating flight controller thread')



    def flight_controller(self):
        try:
            logger.info('Starting flight controller thread for {}'.format(self._cf))
            self._cf.param.set_value('stabilizer.estimator', '2')
            self.reset_estimator(self._cf)

            self._cf.param.set_value('flightmode.posSet', '1')
            print('**PARAMETERS SET**')
            time.sleep(0.1)

            # The threshold for how many frames without tracking
            # is allowed before the cf's motors are stopped
            lost_tracking_threshold = 1000
            frames_without_tracking = 0
            position_hold_timer = 0
            self.circle_angle = 0.0

            # The main flight control loop, the behaviour
            # is controlled by the state of "FlightMode"
            while self.flying_enabled:
                print('cf_pos.x = {}'.format(self.cf_pos.x))
                # print('start of the main control loop')
                # Check that the position is valid and store it
                if self.cf_pos.is_valid():

                    self.valid_cf_pos = self.cf_pos
                    # print('valid cf pos is {}'.format(self.valid_cf_pos))
                    frames_without_tracking = 0
                else:
                    # if it isn't, count number of frames
                    frames_without_tracking += 1
                    # print('frames without tracking {}'.format(frames_without_tracking))

                    if frames_without_tracking > lost_tracking_threshold:
                        self.switch_flight_mode(FlightModeStates.GROUNDED)
                        self.status = "Tracking lost, turning off motors"
                        logger.info(self.status)

                # If the cf is upside down, kill the motors
                if self.flight_mode != FlightModeStates.GROUNDED and (
                        self.valid_cf_pos.roll > 120
                        or self.valid_cf_pos.roll < -120):
                    self.switch_flight_mode(FlightModeStates.GROUNDED)
                    self.status = "Status: Upside down, turning off motors"
                    logger.info(self.status)

                # Switch on the FlightModeState and take actions accordingly
                # Wait so that any on state change actions are completed
                self._event.wait()


                if self.flight_mode == FlightModeStates.LAND:

                    self.send_setpoint(
                        Position(
                            self.current_goal_pos.x,
                            self.current_goal_pos.y,
                            (self.current_goal_pos.z / self.land_rate),
                            yaw=0))
                    # Check if the cf has reached the  position,
                    # if it has set a new position

                    if self.valid_cf_pos.distance_to(
                            Position(self.current_goal_pos.x,
                                     self.current_goal_pos.y,
                                     self.current_goal_pos.z / self.land_rate
                                     )) < self.path_pos_threshold:
                        self.land_rate *= 1.1

                    if self.land_rate > 1000:
                        self.send_setpoint(Position(0, 0, 0))
                        # if self.land_for_recording:
                        #     # Return the control to the recording mode
                        #     # after landing
                        #     mode = FlightModeStates.RECORD
                        #     self.land_for_recording = False
                        # else:
                        #     # Regular landing
                        #     mode = FlightModeStates.GROUNDED
                        mode = FlightModeStates.GROUNDED
                        self.switch_flight_mode(mode)

                elif self.flight_mode == FlightModeStates.PATH:

                    self.send_setpoint(self.current_goal_pos)
                    # Check if the cf has reached the goal position,
                    # if it has set a new goal position
                    if self.valid_cf_pos.distance_to(
                            self.current_goal_pos) < self.path_pos_threshold:

                        if position_hold_timer > self.position_hold_timelimit:

                            current = self.flight_paths[
                                self.pathSelector.currentIndex()]

                            self.path_index += 1
                            if self.path_index == len(current):
                                self.path_index = 1
                            position_hold_timer = 0

                            self.current_goal_pos = Position(
                                current[self.path_index][0],
                                current[self.path_index][1],
                                current[self.path_index][2],
                                yaw=current[self.path_index][3])

                            logger.info('Setting position {}'.format(
                                self.current_goal_pos))
                            self._flight_path_select_row.emit(
                                self.path_index - 1)
                        elif position_hold_timer == 0:

                            time_of_pos_reach = time.time()
                            # Add som time just to get going,
                            # it will be overwritten in the next step.
                            # Setting it higher than the limit
                            # will break the code.
                            position_hold_timer = 0.0001
                        else:
                            position_hold_timer = time.time(
                            ) - time_of_pos_reach

                elif self.flight_mode == FlightModeStates.CIRCLE:
                    self.send_setpoint(self.current_goal_pos)

                    # Check if the cf has reached the goal position,
                    # if it has set a new goal position
                    if self.valid_cf_pos.distance_to(
                            self.current_goal_pos) < self.circle_pos_threshold:

                        if position_hold_timer >= self.position_hold_timelimit:

                            position_hold_timer = 0

                            # increment the angle
                            self.circle_angle = ((self.circle_angle +
                                                  self.circle_resolution)
                                                 % 360)

                            # Calculate the next position in
                            # the circle to fly to
                            self.current_goal_pos = Position(
                                round(
                                    math.cos(math.radians(self.circle_angle)),
                                    4) * self.circle_radius,
                                round(
                                    math.sin(math.radians(self.circle_angle)),
                                    4) * self.circle_radius,
                                self.circle_height,
                                yaw=self.circle_angle)

                            logger.info('Setting position {}'.format(
                                self.current_goal_pos))

                        elif position_hold_timer == 0:

                            time_of_pos_reach = time.time()
                            # Add som time just to get going, it will be
                            # overwritten in the next step.
                            # Setting it higher than the imit will
                            # break the code.
                            position_hold_timer = 0.0001
                        else:
                            position_hold_timer = time.time(
                            ) - time_of_pos_reach

                elif self.flight_mode == FlightModeStates.FOLLOW:

                    if self.wand_pos.is_valid():
                        self.last_valid_wand_pos = self.wand_pos

                        # Fit the angle of the wand in the interval 0-4
                        self.length_from_wand = (2 * (
                            (self.wand_pos.roll + 90) / 180) - 1) + 2
                        self.send_setpoint(
                            Position(
                                self.wand_pos.x + round(
                                    math.cos(math.radians(self.wand_pos.yaw)),
                                    4) * self.length_from_wand,
                                self.wand_pos.y + round(
                                    math.sin(math.radians(self.wand_pos.yaw)),
                                    4) * self.length_from_wand,
                                ((self.wand_pos.z + round(
                                    math.sin(
                                        math.radians(self.wand_pos.pitch)), 4)
                                  * self.length_from_wand) if
                                 ((self.wand_pos.z + round(
                                     math.sin(
                                         math.radians(self.wand_pos.pitch)), 4)
                                   * self.length_from_wand) > 0) else 0)))
                    else:
                        self.length_from_wand = (2 * (
                            (self.last_valid_wand_pos.roll + 90) / 180) -
                                                 1) + 2
                        self.send_setpoint(
                            Position(
                                self.last_valid_wand_pos.x + round(
                                    math.cos(
                                        math.radians(
                                            self.last_valid_wand_pos.yaw)),
                                    4) * self.length_from_wand,
                                self.last_valid_wand_pos.y + round(
                                    math.sin(
                                        math.radians(
                                            self.last_valid_wand_pos.yaw)),
                                    4) * self.length_from_wand,
                                int(self.last_valid_wand_pos.z + round(
                                    math.sin(
                                        math.radians(self.last_valid_wand_pos.
                                                     pitch)), 4) *
                                    self.length_from_wand)))

                elif self.flight_mode == FlightModeStates.LIFT:

                    self.send_setpoint(
                        Position(self.current_goal_pos.x,
                                 self.current_goal_pos.y, 1))

                    if self.valid_cf_pos.distance_to(
                            Position(self.current_goal_pos.x,
                                     self.current_goal_pos.y, 1)) < 0.17:
                        # Wait for hte crazyflie to reach the goal
                        self.switch_flight_mode(FlightModeStates.HOVERING)
                    else:
                        print(self.valid_cf_pos.distance_to(
                            Position(self.current_goal_pos.x,
                                     self.current_goal_pos.y, 1)))

                elif self.flight_mode == FlightModeStates.HOVERING:
                    self.send_setpoint(self.current_goal_pos)

                elif self.flight_mode == FlightModeStates.RECORD:

                    if self.valid_cf_pos.z > 1.0 and not self.recording:
                        # Start recording when the cf is lifted
                        self.recording = True
                        # Start the timer thread
                        self.save_current_position()
                        # Gui
                        self.status = "Recording Flightpath"
                        logger.info(self.status)

                    elif self.valid_cf_pos.z < 0.03 and self.recording:
                        # Stop the recording when the cf is put on
                        # the ground again
                        logger.info("Recording stopped")
                        self.recording = False

                        # Remove the last bit (1s) of the recording,
                        # containing setting the cf down
                        for self.path_index in range(20):
                            self.new_path.pop()

                        # Add the new path to list and Gui
                        now = datetime.datetime.fromtimestamp(time.time())

                        new_name = ("Recording {}/{}/{} {}:{}".format(
                            now.year - 2000, now.month
                            if now.month > 9 else "0{}".format(now.month),
                            now.day if now.day > 9 else "0{}".format(now.day),
                            now.hour if now.hour > 9 else "0{}".format(
                                now.hour), now.minute
                            if now.minute > 9 else "0{}".format(now.minute)))

                        self.new_path.insert(0, new_name)
                        self.flight_paths.append(self.new_path)
                        self._path_selector_add_item.emit(new_name)

                        # Select the new path
                        self._path_selector_set_index.emit(
                            len(self.flight_paths) - 1)
                        self.path_changed()
                        Config().set("flight_paths", self.flight_paths)

                        # Wait while the operator moves away
                        self.status = "Replay in 3s"
                        time.sleep(1)
                        self.status = "Replay in 2s"
                        time.sleep(1)
                        self.status = "Replay in 1s"
                        time.sleep(1)
                        # Switch to path mode and replay the recording
                        self.switch_flight_mode(FlightModeStates.PATH)

                elif self.flight_mode == FlightModeStates.GROUNDED:
                    # # testing multiple drone connection
                    # uri_2 = 'radio://0/80/2M/E7E7E7E7ED'
                    # with SyncCrazyflie(uri_2, cf=Crazyflie(rw_cache='./cache')) as scf:
                    #     print("yeah, I'm connected up!")
                    #     time.sleep(3)
                    #     print("now I will disconnect")
                    pass  # If gounded, the control is switched back to gamepad

                time.sleep(0.001)

        except Exception as err:
            logger.error(err)
            self.cfStatus = str(err)

        logger.info('Terminating flight controller thread')


    """change the state of the state machine (?)"""
    def set_lift_mode(self):
        self.switch_flight_mode(FlightModeStates.LIFT)

    def set_land_mode(self):
        self.switch_flight_mode(FlightModeStates.LAND)

    def set_follow_mode(self):
        # Toggle follow mode on and off

        if self.flight_mode == FlightModeStates.FOLLOW:
            self.switch_flight_mode(FlightModeStates.HOVERING)
        else:
            self.switch_flight_mode(FlightModeStates.FOLLOW)

    def set_kill_engine(self):
        # self.send_setpoint(Position(0, 0, 0))
        self.switch_flight_mode(FlightModeStates.GROUNDED)
        logger.info('Stop button pressed, kill engines')

    def wait_for_position_estimator(self, cf):
        logger.info('Waiting for estimator to find stable position...')

        self.cfStatus = (
            'Waiting for estimator to find stable position... '
            '(QTM needs to be connected and providing data)'
        )

        log_config = LogConfig(name='Kalman Variance', period_in_ms=100)
        log_config.add_variable('kalman.varPX', 'float')
        log_config.add_variable('kalman.varPY', 'float')
        log_config.add_variable('kalman.varPZ', 'float')

        var_y_history = [1000] * 10
        var_x_history = [1000] * 10
        var_z_history = [1000] * 10

        threshold = 0.001

        with SyncLogger(cf, log_config) as log:
            for log_entry in log:
                data = log_entry[1]

                var_x_history.append(data['kalman.varPX'])
                var_x_history.pop(0)
                var_y_history.append(data['kalman.varPY'])
                var_y_history.pop(0)
                var_z_history.append(data['kalman.varPZ'])
                var_z_history.pop(0)

                min_x = min(var_x_history)
                max_x = max(var_x_history)
                min_y = min(var_y_history)
                max_y = max(var_y_history)
                min_z = min(var_z_history)
                max_z = max(var_z_history)

                print("x var = {} y var = {} z var = {}".
                format(max_x - min_x, max_y - min_y, max_z - min_z))

                if (max_x - min_x) < threshold and (
                        max_y - min_y) < threshold and (
                        max_z - min_z) < threshold:
                    logger.info("Position found with error in, x: {}, y: {}, "
                                "z: {}".format(max_x - min_x,
                                               max_y - min_y,
                                               max_z - min_z))

                    self.cfStatus = ": connected"

                    self.switch_flight_mode(FlightModeStates.GROUNDED)

                    break

    def reset_estimator(self, cf):
        # Reset the kalman filter

        cf.param.set_value('kalman.resetEstimation', '1')
        time.sleep(0.1)
        cf.param.set_value('kalman.resetEstimation', '0')

        self.wait_for_position_estimator(cf)

    def switch_flight_mode(self, mode):
        # Handles the behaviour of switching between flight modes
        self.flight_mode = mode

        # Handle client input control.
        # Disable gamepad input if we are not grounded
        if self.flight_mode in [
            FlightModeStates.GROUNDED,
            FlightModeStates.DISCONNECTED,
            FlightModeStates.RECORD
        ]:
            self._helper.mainUI.disable_input(False)
        else:
            self._helper.mainUI.disable_input(True)

        self._event.clear()
        # # Threadsafe call
        self._machine.postEvent(FlightModeEvent(mode))

        logger.info('Switching Flight Mode to: %s', mode)

    def send_setpoint(self, pos):
        # Wraps the send command to the crazyflie

        latest_time = time.perf_counter()
        print('latest time =',latest_time)

        if latest_time - self.last_time < .100:
            return
        elif self._cf is not None:
            self._cf.commander.send_position_setpoint(pos.x, pos.y, pos.z, 0.0)
            self.last_time =  time.perf_counter()

class Position:
    def __init__(self, x, y, z, roll=0.0, pitch=0.0, yaw=0.0):
        self.x = x
        self.y = y
        self.z = z
        self.roll = roll
        self.pitch = pitch
        self.yaw = yaw

    def distance_to(self, other_point):
        return math.sqrt(
            math.pow(self.x - other_point.x, 2) +
            math.pow(self.y - other_point.y, 2) +
            math.pow(self.z - other_point.z, 2))

    def is_valid(self):
        # Checking if the respective values are nan
        # if any of them were nan then the function returs false
        # print('is valid has run')
        return self.x == self.x and self.y == self.y and self.z == self.z

    def __str__(self):
        return "x: {} y: {} z: {} Roll: {} Pitch: {} Yaw: {}".format(
            self.x, self.y, self.z, self.roll, self.pitch, self.yaw)



