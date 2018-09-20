"""mt5 zmq test client."""
import argparse
from collections import namedtuple
import logging
import re
import struct

import requests
import zmq

logger = logging.getLogger(__name__)
logger.info("libzmq: %s", zmq.zmq_version())
logger.info("pyzmq: %s", zmq.pyzmq_version())

# pylint: disable=no-member

Order = namedtuple('Order', ['id', 'price', 'type'])

# Context are thread safe already,
# we'll create one global one for all agents
context = zmq.Context()


class Agent:
  """Base class for Pedlar trading agent."""
  name = "agent"
  polltimeout = 2000 # milliseconds
  csrf_re = re.compile('name="csrf_token" type="hidden" value="(.+)"')

  def __init__(self, username="nobody", password="",
               ticker="tcp://localhost:7000",
               endpoint="http://localhost:5000"):
    self.username = username
    self.password = password
    self.endpoint = endpoint
    self.session = None
    self.ticker = ticker
    self.poller = None
    self.orders = dict()

  @classmethod
  def from_args(cls, parents=None):
    """Create agent instance from command line arguments."""
    parser = argparse.ArgumentParser(description="Pedlar trading agent.", parents=parents or list())
    parser.add_argument("-u", "--username", default="nobody", help="Pedlar Web username.")
    parser.add_argument("-p", "--password", default="", help="Pedlar Web password.")
    parser.add_argument("-t", "--ticker", default="tcp://localhost:7000", help="Ticker endpoint.")
    parser.add_argument("-e", "--endpoint", default="http://localhost:5000", help="Pedlar Web endpoint.")
    return cls(**vars(parser.parse_args()))

  def connect(self):
    """Attempt to connect pedlarweb and ticker endpoints."""
    #-- pedlarweb connection
    # We will adapt to the existing web login rather than
    # creating a new api endpoint for agent requests
    logger.info("Attempting to login to Pedlar web.")
    session = requests.Session()
    try:
      r = session.get(self.endpoint+"/login") # CSRF protected
      r.raise_for_status()
    except:
      logger.critical("Failed to connect to Pedlar web.")
      raise RuntimeError("Connection to Pedlar web failed.")
    try:
      csrf_token = self.csrf_re.search(r.text).group(1)
    except AttributeError:
      raise Exception("Could not find CSRF token in auth.")
    payload = {'username': self.username, 'password': self.password,
               'csrf_token': csrf_token}
    r = session.post(self.endpoint+"/login", data=payload, allow_redirects=False)
    r.raise_for_status()
    if not r.is_redirect or not r.headers['Location'].endswith('/'):
      raise Exception("Failed login into Pedlar web.")
    self.session = session
    logger.info("Pedlar web authentication successful.")
    #-- ticker connection
    socket = context.socket(zmq.SUB)
    # Set topic filter, this is a binary prefix
    # to check for each incoming message
    # set from server as uchar topic = X
    # We'll subsribe to everything for now
    socket.setsockopt(zmq.SUBSCRIBE, bytes())
    # socket.setsockopt(zmq.SUBSCRIBE, bytes.fromhex('00'))
    logger.info("Connecting to ticker: %s", self.ticker)
    socket.connect(self.ticker)
    self.poller = zmq.Poller()
    self.poller.register(socket, zmq.POLLIN)

  def disconnect(self):
    """Close server connection gracefully in any."""
    # Clean up remaining orders
    self.close()
    # Ease the burden on server and logout
    r = self.session.get(self.endpoint+"/logout", allow_redirects=False)
    if not r.is_redirect:
      logger.warn("Could not logout from Pedlar web.")

  def on_order(self, order):
    """Called on successful order."""
    pass

  def talk(self, order_id=0, volume=0.01, action=0):
    """Make a request response attempt to Pedlar web."""
    payload = {'order_id': order_id, 'volume': volume, 'action': action,
               'name': self.name}
    try:
      r = self.session.post(self.endpoint+'/trade', json=payload)
      r.raise_for_status()
      resp = r.json()
    except Exception as e:
      logger.error("Pedlar web communication error: %s", str(e))
      raise IOError("Pedlar web server communication error.")
    return resp

  def _place_order(self, otype="buy", volume=0.01, single=True, reverse=True):
    """Place a buy or a sell order."""
    ootype = "sell" if otype == "buy" else "buy" # Opposite order type
    if (reverse and
        not self.close([oid for oid, o in self.orders.items() if o.type == ootype])):
      # Attempt to close all opposite orders first
      return
    if single and [1 for o in self.orders.values() if o.type == otype]:
      # There is already an order of the same type
      return
    # Request the actual order
    logger.info("Placing a %s order.", otype)
    try:
      resp = self.talk(volume=volume, action=2 if otype == "buy" else 3)
      order = Order(id=resp['order_id'], price=resp['price'], type=otype)
      self.orders[order.id] = order
      self.on_order(order)
    except:
      logger.error("Failed to place %s order.", otype)

  def buy(self, volume=0.01, single=True, reverse=True):
    """Place a new buy order and store it in self.orders
    :param volume: size of trade
    :param single: only place if there is not an already
    :param reverse: close sell orders if any
    """
    self._place_order(otype="buy", volume=volume, single=single, reverse=reverse)

  def sell(self, volume=0.01, single=True, reverse=True):
    """Place a new sell order and store it in self.orders
    :param volume: size of trade
    :param single: only place if there is not an already
    :param reverse: close buy orders if any
    """
    self._place_order(otype="sell", volume=volume, single=single, reverse=reverse)

  def close(self, order_ids=None):
    """Close open all orders or given ids
    :param order_ids: only close these orders
    :return: true on success false otherwise
    """
    oids = order_ids if order_ids is not None else list(self.orders.keys())
    for oid in oids:
      logger.info("Closing order %s", oid)
      try:
        self.talk(order_id=oid, action=1)
        self.orders.pop(oid, None)
      except:
        logger.error("Failed to close order: %s", oid)
        return False
    return True

  def on_tick(self, bid, ask):
    """Called on every tick update.
    :param bid: latest bid price
    :param ask: latest asking price
    """
    pass

  def on_bar(self, bopen, bhigh, blow, bclose):
    """Called on every last bar update.
    :param bopen: opening price
    :param bhigh: highest price
    :param blow: lowest price
    :param bclose: closing price
    """
    pass

  def run(self):
    """Start main loop and receive updates."""
    # Check connection
    if not self.session:
      self.connect()
    # We'll trade forever until interrupted
    logger.info("Starting main trading loop...")
    try:
      while True:
        socks = self.poller.poll(self.polltimeout)
        if not socks:
          continue
        raw = socks[0][0].recv()
        # unpack bytes https://docs.python.org/3/library/struct.html
        if len(raw) == 17:
          # We have tick data
          bid, ask = struct.unpack_from('dd', raw, 1) # offset topic
          self.on_tick(bid, ask)
        elif len(raw) == 33:
          # We have bar data
          bo, bh, bl, bc = struct.unpack_from('dddd', raw, 1) # offset topic
          self.on_bar(bo, bh, bl, bc)
    finally:
      logger.info("Stopping agent...")
      self.disconnect()