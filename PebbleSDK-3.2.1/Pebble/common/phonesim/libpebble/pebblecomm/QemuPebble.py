import logging
import struct
import time
import socket
import select
import os

# These protocol IDs are defined in qemu_serial.h in the tintin project
QemuProtocol_SPP = 1                    # Send SPP data (used for Pebble protocol)
QemuProtocol_Tap = 2                    # Send a tap event
QemuProtocol_BluetoothConnection = 3    # Send a bluetooth connection event
QemuProtocol_Compass = 4                # Send a compass event
QemuProtocol_Battery = 5                # Send a battery info event
QemuProtocol_Accel = 6                  # Send a accel data event
QemuProtocol_VibrationNotification = 7  # Vibration notification from Pebble
QemuProtocol_Button = 8                 # Send a button state change 

QEMU_HEADER_SIGNATURE = 0xFEED
QEMU_FOOTER_SIGNATURE = 0xBEEF
QEMU_MAX_DATA_LEN = 2048

class QemuPebble(object):

    def __init__(self, host, port, timeout=1, connect_timeout=5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.socket = None
        self.hdr_format = "!HHH"
        self.footer_format = "!H"
        self.hdr_size = struct.calcsize(self.hdr_format)
        self.footer_size = struct.calcsize(self.footer_format)
        self.max_packet_size = QEMU_MAX_DATA_LEN + self.hdr_size + self.footer_size
        self.assembled_data = ''
        self.trace_enabled = False

    def enable_trace(self, setting):
        self.trace_enabled = setting

    def connect(self):
        start_time = time.time()
        connected = False
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        while not connected and (time.time() - start_time < self.connect_timeout):
            try:
                self.socket.connect((self.host, self.port))
                connected = True
            except socket.error:
                self.socket.close()
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                time.sleep(0.1)

        if not connected:
            logging.error("Unable to connect to emulator at %s:%s. Is it running?" % (self.host,
                            self.port))
            os._exit(-1)


        logging.info("Connected to emulator at %s:%s" % (self.host, self.port))


    def write(self, payload, protocol=QemuProtocol_SPP):

        # Append header and footer to the payload
        data = (struct.pack(self.hdr_format, QEMU_HEADER_SIGNATURE, protocol, len(payload))
                    + payload + struct.pack(self.footer_format, QEMU_FOOTER_SIGNATURE))

        self.socket.send(data)
        if self.trace_enabled:
            logging.debug('send>>> ' + data.encode('hex'))

    def read(self):
        """
        retval:   (source, topic, response, data)
            source can be either 'ws' or 'watch'
            if source is 'watch', then this is a pebble protocol packet and topic is the endpoint
                      identifier
            if source is 'ws', then topic is either 'status','phoneInfo',
                      'watchConnectionStatusUpdate' or 'log'
            if source is 'qemu', then topic is the QemuProtocol_.* enum

        """
        # socket timeouts for asynchronous operation is normal.  In this
        # case we shall return all None to let the caller know.
        try:
            readable, writable, errored = select.select([self.socket], [], [], self.timeout)
        except select.error:
            return (None, None, None, None)

        if not readable:
            return (None, None, None, None)

        data = self.socket.recv(self.max_packet_size)
        if not data:
            logging.error("emulator disconnected")
            os._exit(-1)

        if self.trace_enabled:
            logging.debug('rcv<<< ' + data.encode('hex'))
        self.assembled_data += data

        # Look for a complete packet
        while len(self.assembled_data) >= self.hdr_size:
            (signature, protocol, data_len) = struct.unpack(self.hdr_format,
                                                      self.assembled_data[0:self.hdr_size])
            if signature != QEMU_HEADER_SIGNATURE:
                self.assembled_data = self.assembled_data[1:]
                logging.debug("Skipping garbage byte")
                continue

            # Check for valid data len
            if data_len > QEMU_MAX_DATA_LEN:
                logging.warning("Invalid packet len detected: %d" % data_len)
                # Skip past this header and look for another one
                self.assembled_data = self.assembled_data[1:]
                continue

            # If not a complete packet, break out
            if len(self.assembled_data) < self.hdr_size + data_len + self.footer_size:
                break

            # Pull out the packet
            data = self.assembled_data[self.hdr_size:data_len+self.hdr_size]
            self.assembled_data = self.assembled_data[self.hdr_size + data_len + self.footer_size:]

            # Ignore everything but SPP protocol for now
            if protocol == QemuProtocol_SPP:
                return ('watch', 'Pebble Protocol', data, data)
            else:
                return ('qemu', protocol, data, data)

        # If we broke out, we don't have a complete packet yet
        return (None, None, None, None)

    def close(self):
        """ Closes the socket connection. """
        self.socket.close()

