#!/usr/bin/env python
#
# Timothy O'Malley <timo@alum.mit.edu> 2003
# Modified by RMZ 2005-2011
#

import logging, struct, socket, select, time, thread, string, sys, re, os
from StringIO import StringIO

# logging definition
console = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s %(message)s')
console.setFormatter(formatter)
logging.getLogger('').addHandler(console)
logging.getLogger('').setLevel(logging.DEBUG)

TFTP_PORT = 69
HOME_BASE = "/opt/tftp/"


class TFTPError(Exception):
    pass


#
# A class for a TFTP Connection
#
class TFTPConnection:
    
    RRQ  = 1
    WRQ  = 2
    DATA = 3
    ACK  = 4
    ERR  = 5
    HDRSIZE = 4  # number of bytes for OPCODE and BLOCK in header

    def __init__(self, sock, remote_addr, blocksize=512, timeout=5.0, retry=2 ):
        self.remote_addr = remote_addr

        self.blocksize = blocksize
        self.timeout   = timeout
        self.retry     = retry
        self.sock        = sock
        self.active      = 0
        self.blockNumber = 0
        self.lastpkt     = ""
        self.mode        = ""
        self.filename    = ""
        self.tx_queue    = None
        self.file        = None
    # end __init__

    def timeout_check (self):
        if time.time() - self.last_tx_rx >= self.timeout:
            if self.retry:
                self.retry = self.retry - 1
                self.retransmit()
            else:
                # No more retries
                raise TFTPError (2, "Client is gone")
        
        return 0
    
    def bind(self, host="", port=0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock = sock
        if host or port:
            sock.bind((host, port))
    # end start
    
    def send(self, pkt=""):
        self.sock.sendto(pkt, self.remote_addr)
        self.lastpkt = pkt
        self.last_tx_rx = time.time()
    # end send

    def recv(self, data):
        self.last_tx_rx = time.time()
        pkt = self.parse(data)
#       print "got PKT", repr(pkt)
        opcode = pkt["opcode"]
        if opcode == self.RRQ:
            self.handleRRQ(pkt)
        elif opcode == self.WRQ:
            self.handleWRQ(pkt)
        elif opcode == self.ACK:
            self.handleACK(pkt)
        elif opcode == self.ERR:
            self.handleERR(pkt)
        else:
            raise TFTPError(4, "Unknown packet type " + str(opcode))

        # Not active : our reply must be error, so don't wait for ACK
        if not self.active:
                raise TFTPError (5, "Connection is over")
                
        return

        # Never run after that point
        sock        = self.sock
        F           = sock.fileno()
        client_addr = self.client_addr
        timeout     = self.timeout            
        retry       = self.retry

        while retry:
            r,w,e = select.select( [F], [], [F], timeout)
            if not r:
                # We timed out -- retransmit
                retry = retry - 1
                self.retransmit()
            else:
                # Read data packet
                pktsize = self.blocksize + self.HDRSIZE
                data, addr = sock.recvfrom(pktsize)
                if addr == client_addr:
                    break
        else:
            raise TFTPError(4, "Transfer timed out")
        # end while
        
        return self.parse(data)
    # end recv

    def parse(self, data, unpack=struct.unpack):
        buf = buffer(data)
        pkt = {}

        try:   
            opcode = pkt["opcode"] = unpack("!H", buf[:2])[0]
        except struct.error:
            raise TFTPError(4, "Unknown packet type") 

        if (opcode == self.RRQ) or (opcode == self.WRQ):
            filename, mode, junk  = string.split(data[2:], "\000", 2)
            pkt["filename"] = filename
            pkt["mode"]     = mode
            params      = string.split (junk, "\000")
            # Makes the RRQ parameters a dictionnary (key: value)
            pkt["params"] = dict(zip(params[::2], params[1::2]))
        elif opcode == self.DATA:
            block  = pkt["block"] = unpack("!H", buf[2:4])[0]
            data   = pkt["data"]  = buf[4:]
        elif opcode == self.ACK:
            block  = pkt["block"] = unpack("!H", buf[2:4])[0]
        elif opcode == self.ERR:
            errnum = pkt["errnum"] = unpack("!H", buf[2:4])[0]
            errtxt = pkt["errtxt"] = buf[4:-1]
        else:
            raise TFTPError(4, "Unknown packet type")

        return pkt
    # end parse

    def retransmit(self):
        self.sock.sendto(self.lastpkt, self.remote_addr)
        self.last_tx_rx = time.time()
        return
    # end retransmit

    def recvData(self, pkt):
        if pkt["block"] == self.blockNumber:
            # We received the correct DATA packet
            self.active = ( self.blocksize == len(pkt["data"]) )
            self.handleDATA(pkt)
        return
    # end recvData

    def sendData(self, data, pack=struct.pack):
        blocksize = self.blocksize
        block     = self.blockNumber = self.blockNumber + 1
        lendata   = len(data)
        format = "!HH%ds" % lendata
        pkt = pack(format, self.DATA, block, data)
        self.send(pkt)
        return len(data)
    # end sendData

    def sendAck(self, pack=struct.pack):
        block            = self.blockNumber
        self.blockNumber = self.blockNumber + 1
        format = "!HH"
        pkt = pack(format, self.ACK, block)
        self.send(pkt)
    # end sendAck
        
    def sendError(self, errnum, errtext, pack=struct.pack):
        errtext = errtext + "\000"
        format = "!HH%ds" % len(errtext)
        outdata = pack(format, self.ERR, errnum, errtext)
        self.sock.sendto(outdata, self.remote_addr)
        self.lastpkt = outdata
        self.last_tx_rx = time.time()
        return
    # end sendError

    #
    def handleRRQ(self, pkt):

        filename = pkt["filename"]
        mode = pkt["mode"]
        logging.info('RRQ %s mode %s %s',filename,mode,repr(self.remote_addr))

        try:
                self.file = file (HOME_BASE + filename)
                logging.info('Sending %s %s',HOME_BASE+filename,repr(self.remote_addr))
        except IOError, OSError:
                self.file = None
                logging.info('No such file : %s %s',filename,repr(self.remote_addr))
                self.sendError (1, "No such file")

        if self.tx_queue:
            self.sendData (self.tx_queue[:self.blocksize])
            self.tx_queue = self.tx_queue[self.blocksize:]
            self.active = 1
        elif self.file:
            s = self.sendData(self.file.read(self.blocksize))
            self.active = 1
        else:
            self.active = 0

        return

    #
    def handleWRQ(self, pkt):
        filename  = pkt["filename"]
        mode      = pkt["mode"]
        logging.info('WRQ %s mode %s %s',filename,mode,repr(self.remote_addr))
        self.sendError (1, "Not implemented")
        self.active = 0
        return
    # end writeFile

    def handleACK(self, pkt):

        if pkt["block"] == self.blockNumber and self.active:
            data = self.file.read(self.blocksize)
            if len(data):
                # Send one more chunk
                l = self.sendData(data)
            else:
                logging.info('File succefully send, last ack : %s %s',self.blockNumber,repr(self.remote_addr))
                l = self.sendData("")
                # If this is the last one, no more active
                self.active = 0
        elif self.active:
            self.retransmit()

        return
    # end handleACK
    
    def handleDATA(self, pkt):
        self.sendAck()
        data = pkt["data"]
        self.file.write( data )
    # end handleDATA
        
    def handleERR(self, pkt):
        logging.info('ERR %s %s %s',pkt["errnum"],pkt["errtxt"],repr(self.remote_addr))
        self.active = 0
        return
    # end handleERR

# end class TFTPConnection


#
# Simple TFTP Server
# 
class TFTPServer:

    """TFTP Server
    Implements a NAT friendly TFTP Server.
    """

    def __init__(self, host="", port=TFTP_PORT, conn=TFTPConnection ):
        self.host = host
        self.port = port
        self.conn = conn
        self.nbconn = 0
        self.sock = None
        self.conn_list = { }
        self.bind(host, port)
    # end __init__

    def bind(self, host, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock = sock
        sock.bind((host, port))
    # end start

    def forever(self):
        F = self.sock.fileno()
                
        while 1:
            r,w,e = select.select( [F], [], [F], 1.0)
            if not r:
                # Time out check
                for peer in enumerate(self.conn_list):
                    # Deletes the connection
                    try:
                        self.conn_list[peer[1]].timeout_check()
                    except TFTPError:
                        self.nbconn = self.nbconn - 1
                        logging.info('Del (timeout) connection from %s %s active connections',repr(peer[1]),self.nbconn)
                        del self.conn_list[peer[1]]
                        break
                        
            else:        
                data, addr = self.sock.recvfrom(516)
                # Do we know this remote peer ?
                if not self.conn_list.has_key (addr):
                    # No, create a new peer
                    self.nbconn = self.nbconn + 1
                    logging.info("New connection from %s %s active connections",repr(addr),self.nbconn)
                    self.conn_list[addr] = self.conn (self.sock, addr)
                    # Pass data
                    try:
                        self.conn_list[addr].recv (data)
                    except TFTPError:
                        self.nbconn = self.nbconn - 1
                        logging.info('Del connection from %s %s active connections',repr(addr),self.nbconn)
                        del self.conn_list[addr]
            
    # end forever
# end class TFTPServer

if __name__ == "__main__":

    try:
        serv = TFTPServer( "", TFTP_PORT  )
        logging.info('Starting on port %s',TFTP_PORT)
        serv.forever()
    except KeyboardInterrupt, SystemExit:
        pass
