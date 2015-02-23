"""
Automated requests we should respond to:
version -> verack, reject
ping -> pong, reject

Requests that can be made:
getaddr -> addr, reject
getblocks -> inv (block #1 if not found), reject
getheaders -> headers (block #1 if not found), reject
mempool -> inv, reject
getdata -> tx, block, notfound, reject
"""
from twisted.internet.protocol import Protocol
from twisted.internet import defer, reactor
from twisted.protocols.policies import TimeoutMixin
from twisted.python import log

from coinbits.clients import ProtocolBuffer
from coinbits.protocol.serializers import Pong, VerAck, GetData, GetBlocks,\
     Version, Inventory, GetAddr, MemPool
from coinbits.protocol import fields

from txbitcoin import utils


class MessageRejected(Exception):
    """
    Message was rejected by peer.
    """


class Command(object):
    def __init__(self, message, cmdlist, expect=None, timeout=10):
        self.message = message
        self.expect = expect or []
        self._deferred = defer.Deferred()
        terror = defer.TimeoutError("Message %s response timeout" % message.command)
        self.timeoutCall = reactor.callLater(timeout, self.fail, terror)
        self.cmdlist = cmdlist

    def success(self, value):
        if self.timeoutCall.active():
            self.timeoutCall.cancel()
        self._deferred.callback(value)
        self.called = True

    def fail(self, error):
        if self.timeoutCall.active():
            self.timeoutCall.cancel()
        # if we're failing due to a timeout, remove from cmd list
        if self in self.cmdlist:
            self.cmdlist.remove(self)
        self._deferred.errback(error)
        self.called = True        


class BitcoinProtocol(Protocol, TimeoutMixin):
    def __init__(self, timeOut=10, userAgent=None):
        self.userAgent = userAgent or "/txbitcoin:0.0.1/"
        self._current = []
        self.persistentTimeOut = self.timeOut = timeOut
    
    def makeConnection(self, transport):
        Protocol.makeConnection(self, transport)
        self._buffer = ProtocolBuffer()

    def connectionMade(self):
        v = Version()
        v.user_agent = self.userAgent
        binmsg = v.get_message()
        self.transport.write(binmsg)

    def timeoutConnection(self):
        """
        Close the connection in case of timeout.
        """
        self._cancelCommands(defer.TimeoutError("Connection timeout"))
        self.transport.loseConnection()

    def connectionLost(self, reason):
        self._cancelCommands(reason)

    def _cancelCommands(self, reason):
        """
        Cancel all the outstanding commands, making them fail with reason.
        """
        while self._current:
            cmd = self._current.pop(0)
            cmd.fail(reason)

    def send_message(self, message, *expect):
        if not self._current:
            self.setTimeout(self.persistentTimeOut)
        log.msg("Sending %s command" % message.command)
        binmsg = message.get_message()
        self.transport.write(binmsg)
        cmd = Command(message, self._current, expect)
        self._current.append(cmd)
        return cmd._deferred

    def dataReceived(self, data):
        self._buffer.write(data)
        header, message = self._buffer.receive_message()
        if message is None:
            return

        log.msg("[%s] Received %s command" % (self.factory.addr, header.command))
        mname = "handle_%s" % header.command
        cmd = getattr(self, mname, None)
        if cmd is None:
            return

        self.resetTimeout()
        cmd(message)
        # if no pending request, remove timeout
        if not self._current:
            self.setTimeout(None)

    def handle_version(self, message):
        binmsg = VerAck().get_message()
        self.transport.write(binmsg)

    def handle_ping(self, message):
        pong = Pong()
        pong.nonce = message.nonce
        binmsg = pong.get_message()
        self.transport.write(binmsg)

    def handle_verack(self, message):
        # our connection isn't ready for messages
        # until after version -> verack exchange
        self.factory.connectionMade()

    def _popMatchingCmd(self, cmdname):
        for index, cmd in enumerate(self._current):
            if cmdname in cmd.expect:
                return self._current.pop(index)
        return None

    def handle_notfound(self, message):
        """
        Not exactly a failure, so return None to
        the last command's defered.
        """
        cmd = self._popMatchingCmd('notfound')
        if cmd is not None:
            cmd.success(None)

    def handle_reject(self, message):
        cmd = self._popMatchingCmd('reject')
        if cmd is not None:
            cmd.fail(MessageRejected(message.reason))

    def _generic_handler(self, message):
        cmd = self._popMatchingCmd(message.command)
        if cmd is not None:
            cmd.success(message)

    handle_inv = _generic_handler
    handle_block = _generic_handler
    handle_tx = _generic_handler
    handle_addr = _generic_handler
    handle_headers = _generic_handler

    def getBlockList(self, blocks):
        blocks = utils.hashes_to_ints(blocks)
        gb = GetBlocks(blocks)
        return self.send_message(gb, 'inv')

    def sendTransaction(self, tx):
        binmsg = tx.get_message()
        self.transport.write(binmsg)

    def getPeers(self):
        getaddr = GetAddr()
        return self.send_message(getaddr, 'addr')

    def getHeaders(self, blocks):
        blocks = utils.hashes_to_ints(blocks)
        gh = GetHeaders(blocks)
        return self.send_message(gh, 'headers')

    def getMemPool(self):
        mp = MemPool()
        return self.send_message(mp, 'inv')

    def getBlockData(self, hashes):
        return self._getData('MSG_BLOCK', hashes, 'block', 'notfound')

    def getTxnData(self, hashes):
        return self._getData('MSG_TX', hashes, 'tx', 'notfound')

    def _getData(self, type, hashes, *expect):
        gd = GetData()
        for h in utils.hashes_to_ints(hashes):
            inv = Inventory()
            inv.inv_type = fields.INVENTORY_TYPE[type]
            inv.inv_hash = h
            gd.inventory.append(inv)
        return self.send_message(gd, *expect)
