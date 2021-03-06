#coding=utf-8

import sys, errno
from uuid import uuid1

from log import log
from schedule import *
from protocol import BaseProtocol
from logic import *
from conn import *
from util import Timer



class TcpServerAction(object):
    """
        action for tcpserver:
            listen, recv, and response
    """
    def __init__(self, listen_addr, name = ''):
        self.addr = listen_addr
        self.server = None
        self.name = name if name else 'Port-%s' % listen_addr[1] 
        self.log = log
        self.protocol = BaseProtocol()
        self.logic = BaseLogic()


    def reg_protocol(self, protocol):
        self.protocol = protocol


    def reg_logic(self, logic):
        self.logic = logic


    def tcp_listen(self):
        """ register tcp listen in server"""
        fd = self.server.tcp_listen(self.addr)
        self.server.wait_read(fd, self.event_tcplisten, fd)


    @logic_schedule(True)
    def event_tcplisten(self, fd):
        """ event tcplisten callback """
        uid, addr = self.server.event_tcplisten(fd)
        if uid == -1:
            yield creturn() 

        conn.save_uid(uid, self)
        self.log.debug("%s: Accept connection from %s, %d, fd = %d uid = %s" \
                        % (self.name, addr[0], addr[1], fd, uid[:8])) 
        
        yield self.new_connection(uid)
        yield creturn()


    @logic_schedule()
    def recving(self, uid, timeout = -1):
        """ return data, True/False (fd is closed?) """
        status, data = self.server.event_read(uid, timeout)
        if status == 1 :
            data = yield status
        elif status == -1:
            yield creturn('', True)
        elif status == 0:
            self.log.debug("[%s] :[%s] received buf :%s" % (self.name, uid[:8], data))
            yield creturn(data, False)
            
        if data[0]:
            self.log.debug("[%s] :[%s] received :%s" % (self.name, uid[:8], data[0]))
        yield creturn(data)
        

    @logic_schedule()
    def sending(self, uid, data):
        """ return True/False """
        data = self.protocol.packet(data)
        status = self.server.event_write(uid, data)
        if status:
            result = yield status
        else:
            yield creturn(False)
            
        if result:
            self.log.debug("[%s] :[%s] send done :%s" % (self.name, uid[:8], data))

        yield creturn(result)



    @logic_schedule()
    def new_connection(self, uid):
        """ dealing new accepted connection """

        status = yield self.protocol.handshake(uid)
        if status == False:
            conn.close(uid)
            yield creturn()

        recvdata = ''
 
        while 1:
            data, isclosed = yield self.recving(uid)
            recvdata += data
            
            if isclosed:
                yield self.protocol.close(uid)
                yield self.logic.close(uid)
                yield creturn()

            loop = 1
            while loop:
                result, recvdata, loop = yield self.protocol.handle(recvdata, uid)
                if result == None:
                    break        
                    
                data = yield self.logic.dispatch(result, uid)
                if data: 
                    yield self.sending(uid, data)
                
        yield creturn()


    def clear(self, uid):
        pass



class TcpClientAction(object):
    """
        action for tcp client:
            send request and get response, such as memcache and db
    """

    def __init__(self, connect_addr, name, num = 3):
        self.addr = connect_addr
        self.server = None
        self.name = name
        self.num = num
        self.log = log
        self.protocol = BaseProtocol()
        self.logic = BaseLogic()
        self.conn_pool = []
        self.wait_list = []
        self.signame = self.name + 'requestfd'
        self.ready = False
        conn.save(self)


    def init(self):
        pass


    def reg_protocol(self, protocol):
        self.protocol = protocol


    def reg_logic(self, logic):
        self.logic = logic


    @logic_schedule(True)
    def create_pool(self):
        """ create a connection pool """
        self.init()
        conn_pool = []
        
        while len(conn_pool) < self.num:
            uid = yield self.connection()
            if uid != -1:
                conn_pool.append(uid)
       
        self.conn_pool = conn_pool

        self.ready = True
        yield creturn()



    @logic_schedule()
    def connection(self):
        uid = yield self.connect(self.addr)
        if uid == -1:
            yield self.server.set_timer(schedule_sleep(5))
            yield creturn(-1)

        status = yield self.protocol.handshake(uid)
        if status == False:
            conn.close(uid)
            yield self.server.set_timer(schedule_sleep(5))
            uid = -1 

        yield creturn(uid)


    
    @logic_schedule()
    def connect(self, addr):
        uid = self.server.reg_tcp_connect(self.addr)

        if uid == -1: 
            self.log.error("[%s] uid: [%s] connect %s error" % (self.name, uid[:8], addr))
            yield creturn(-1)

        self.log.debug("[%s] uid: [%s] Try to connect %s" % (self.name, uid[:8], addr))
        err_no = yield 

        if err_no in (errno.ECONNREFUSED, errno.ETIMEDOUT):
            self.log.error("%s %s Connection refused. Let's wait a moment to retry..." \
                            % (self.name, self.addr))
            yield creturn(-1)

        conn.save_uid(uid, self)
        self.log.debug("[%s] uid: [%s] Connect to %s success" % (self.name, uid[:8], addr))
     
        yield creturn(uid)


    @logic_schedule()
    def request(self, data, timeout = -1):
        """ send request and get response """
        while len(self.conn_pool) == 0:
            yield schedule_waitsignal(self.signame)
            continue

        uid = self.conn_pool.pop()

        status = yield self.sending(uid, data)
        if status == False:
            self.repair()
            yield creturn(False, None)

        recvdata = ''
 
        while 1:
            data, isclosed = yield self.recving(uid, timeout)
            recvdata += data

            if isclosed:
                yield self.protocol.close(uid)
                yield self.logic.close(uid)
                self.repair()
                yield creturn(False, None)

            loop = 1
            result, _, _ = yield self.protocol.handle(recvdata, uid)
                    
            data = yield self.logic.dispatch(result, uid)
            if result:
                break
        
        
        schedule_notify(self.signame)
        self.conn_pool.append(uid)
        yield creturn(True, data)


    def repair(self):
        self.log.debug("Repair")
        tm = Timer(5, self.reconnect)
        self.server.set_timer(tm)


    def reconnect(self):
        self._reconnect()


    @logic_schedule(True)
    def _reconnect(self):
        while 1:
            uid = yield self.connection()
            if uid != -1:
                break

        schedule_notify(self.signame)
        self.conn_pool.append(uid)
        yield creturn()
        
         

    @logic_schedule()
    def recving(self, uid, timeout = -1):
        """ return data, True/False (fd is closed?) """
        status, data = self.server.event_read(uid, timeout)
        if status == 1 :
            data = yield status
        elif status == -1:
            yield creturn('', True)
        elif status == 0:
            self.log.debug("[%s] :[%s] received :%s" % (self.name, uid[:8], data))
            yield creturn(data, False)
            
        if data[0]:
            self.log.debug("[%s] :[%s] received :%s" % (self.name, uid[:8], data[0]))

        yield creturn(data)
        

    @logic_schedule()
    def sending(self, uid, data):
        """ return True/False """
        data = self.protocol.packet(data)
        status = self.server.event_write(uid, data)
        if status:
            result = yield status
        else:
            yield creturn(False)
            
        if result:
            self.log.debug("[%s] :[%s] send done :%s" % (self.name, uid[:8], data))

        yield creturn(result)


    def clear(self, uid):
        if uid in self.conn_pool:
            self.conn_pool.remove(uid)
            self.repair()




__all__ = ['TcpServerAction', 'TcpClientAction']
