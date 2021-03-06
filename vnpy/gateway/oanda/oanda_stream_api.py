import time

from copy import copy
from dataclasses import dataclass
from functools import partial
from http.client import IncompleteRead, RemoteDisconnected
from typing import Callable, TYPE_CHECKING, Type
from datetime import datetime, timedelta
from threading import Thread
from urllib3.exceptions import ProtocolError
from requests.exceptions import ConnectTimeout, ReadTimeout

from vnpy.api.rest import Request
from vnpy.trader.constant import Exchange, Interval, Offset, Status
from vnpy.trader.object import OrderData, SubscribeRequest, TickData, TradeData
from .oanda_api_base import OandaApiBase
from .oanda_common import (parse_datetime, parse_time)

if TYPE_CHECKING:
    from vnpy.gateway.oanda import OandaGateway
_ = lambda x: x  # noqa

HOST = "https://stream-fxtrade.oanda.com"
TEST_HOST = "https://stream-fxpractice.oanda.com"

# asked from official developer
PRICE_TICKS = {
    "BTCUSD": 0.5,
    "ETHUSD": 0.05,
    "EOSUSD": 0.001,
    "XRPUSD": 0.0001,
}


@dataclass()
class HistoryDataNextInfo:
    symbol: str
    interval: Interval
    end: int


class RequestFailedException(Exception):
    pass


class OandaStreamApi(OandaApiBase):
    """
    Oanda Streaming API
    """

    def __init__(self, gateway: "OandaGateway"):
        """"""
        super().__init__(gateway)

        self.fully_initialized = False
        self.latest_stream_time = dict()
        self.trans_latest_stream_time = dict()
        self.after_subscribe = False
        self.already_init_check = False

        self._transaction_callbacks = {
            'ORDER_FILL': self.on_order_filled,
            'MARKET_ORDER': self.on_order,
            'LIMIT_ORDER': self.on_order,
            'STOP_ORDER': self.on_order,
            'ORDER_CANCEL': self.on_order_canceled,

            # 'HEARTBEAT': do_nothing,
        }

    def connect(
        self,
        key: str,
        session_number: int,
        server: str,
        proxy_host: str,
        proxy_port: int,
    ):
        """
        Initialize connection to REST server.
        """
        self.key = key

        if server == "REAL":
            self.init(HOST, proxy_host, proxy_port)
        else:
            self.init(TEST_HOST, proxy_host, proxy_port)

        self.start(session_number)

        self.gateway.write_log(_("Streaming API启动成功"))

    def subscribe(self, req: SubscribeRequest):
        # noinspection PyTypeChecker
        def _on_price(symbol):
            def __on_price(data: dict, request: Request):
                self.on_price(symbol, data, request)
            return __on_price
        self.add_streaming_request(
            "GET",
            f"/v3/accounts/{self.gateway.account_id}/pricing/stream?instruments={req.symbol}",
            callback=_on_price(req.symbol),
            on_connected=partial(self._start_connection_checker, partial(self.subscribe, copy(req)), copy(req)),
            on_error=partial(self.on_streaming_error, partial(self.subscribe, copy(req))),
            headers={"Accept-Encoding":"gzip, deflate"},
        )
    
    def _start_connection_checker(self, re_subscribe: Callable, req: SubscribeRequest, request: Request):
        if not self.already_init_check:
            self.gateway.write_log("stream connection checker start in sub")
            self.already_init_check = True
            self.after_subscribe = True
            
            self.latest_stream_time[req.symbol] = datetime.now()
            th = Thread(
                target=self.connection_checker,
                args=[re_subscribe, req,],
            )
            th.start()

            if self.gateway.account_id in self.trans_latest_stream_time.keys():
                # already started
                return
            self.subscribe_transaction()

    def connection_checker(self, re_subscribe: Callable, req: SubscribeRequest):
        if self.after_subscribe:
            self.gateway.write_log("stream connection checker start")
            while True:
                now = datetime.now()
                latest = self.latest_stream_time.get(req.symbol)
                latest = latest if latest is not None else datetime.now()
                delta = now - latest

                # self.gateway.write_log("stream connection checker delta is %s seconds" % delta)
                if delta > timedelta(seconds=10):
                    self.gateway.write_log("stream connection checker reconnected due to %ss" % delta)
                    re_subscribe()

                time.sleep(1)

                latest = self.trans_latest_stream_time.get(self.gateway.account_id)
                latest = latest if latest is not None else datetime.now()
                delta = now - latest
                if delta > timedelta(seconds=30):
                    self.gateway.write_log("transaction stream connection checker reconnected due to %ss" % delta)
                    self.subscribe_transaction()

    def on_price(self, symbol: str, data: dict, request: Request):
        type_ = data['type']
        if type_ == 'PRICE':
            symbol = data['instrument']
            # only one level of bids/asks
            bid = data['bids'][0]
            ask = data['asks'][0]
            float_point = len(bid['price'].split(".")[1])
            round_add = 1.0 / float_point * 10 * 5
            print("bid volume, ask volume" %s (bid['liquidity'], ask['liquidity']))
            tick = TickData(
                gateway_name=self.gateway_name,
                symbol=symbol,
                exchange=Exchange.OANDA,
                datetime=parse_datetime(data['time']),
                name=symbol,
                last_price=round((float(bid['price'])+float(ask['price']) + round_add, float_point) / 2),
                bid_price_1=float(bid['price']),
                bid_volume_1=bid['liquidity'],
                ask_price_1=float(ask['price']),
                ask_volume_1=ask['liquidity'],
                volume=round((float(ask['liquidity'])+float(bid['liquidity']) + 0.5)/ 2),
            )
            self.gateway.on_tick(tick)
        self.latest_stream_time[symbol] = datetime.now()

    def has_error(self, target_type: Type[Exception], e: Exception):
        """check if error type \a target_error exists inside \a e"""
        if isinstance(e, target_type):
            return True
        for arg in e.args:
            if isinstance(arg, Exception) and self.has_error(target_type, arg):
                return True
        return False

    def on_streaming_error(self,
                           re_subscribe: Callable,
                           exception_type: type,
                           exception_value: Exception,
                           tb,
                           request: Request,
                           ):
        """normally triggered by network error."""
        # skip known errors
        self.gateway.write_log("ERRRRRROOOOOORRRRR")
        known = False
        for et in (ProtocolError, IncompleteRead, RemoteDisconnected, ConnectTimeout, ReadTimeout,):
            if self.has_error(et, exception_value):
                self.gateway.write_log("Know ERR")
                known = True
                break

        if known:
            # re-subscribe
            re_subscribe()
        # write log for any unknown errors
        else:
            re_subscribe()
            self.gateway.write_log("UNKnow ERR")
            super().on_error(exception_type, exception_value, tb, request)

    def subscribe_transaction(self):
        # noinspection PyTypeChecker
        self.gateway.write_log("subscribe for transaction for account %s" % self.gateway.account_id)
        self.add_streaming_request(
            "GET",
            f"/v3/accounts/{self.gateway.account_id}/transactions/stream",
            callback=self.on_transaction,
            on_connected=self.on_subscribed_transaction,
            on_error=partial(self.on_streaming_error, partial(self.subscribe_transaction, )),
            headers={"Accept-Encoding":"gzip, deflate"},
        )

    def on_subscribed_transaction(self, request: "Request"):
        self.fully_initialized = True
        self.trans_latest_stream_time[self.gateway.account_id] = datetime.now()

    def on_transaction(self, data: dict, request: "Request"):
        type_ = data['type']
        callback = self._transaction_callbacks.get(type_, None)
        if callback is not None:
            callback(data, request)
        elif type_ != "HEARTBEAT":
            print(type_)

        self.trans_latest_stream_time[self.gateway.account_id] = datetime.now()

    def on_order(self, data: dict, request: "Request"):
        order = self.gateway.parse_order_data(data,
                                              Status.NOTTRADED,
                                              'time',
                                              )
        self.gateway.on_order(order)

    def on_order_canceled(self, data: dict, request: "Request"):
        order_id = data.get('clientOrderID', None)
        if order_id is None:
            order_id = data['id']
        order = self.gateway.orders[order_id]
        order.status = Status.CANCELLED
        order.time = parse_time(data['time'])
        self.gateway.on_order(order)

    def on_order_filled(self, data: dict, request: "Request"):
        order_id = data.get('clientOrderID', None)
        if order_id is None:
            order_id = data['orderID']

        order: OrderData = self.gateway.orders[order_id]

        # # new API:
        # price = 0.0
        # if 'tradeOpened' in data:
        #     price += float(data['tradeOpened']['price'])
        # if 'tradeReduced' in data:
        #     price += float(data['tradeReduced']['price'])
        # if 'tradeClosed' in data:
        #     price += sum([float(i['price']) for i in data['tradeClosed']])

        # note: 'price' record is Deprecated
        # but since this is faster and easier, we use this record.
        price = float(data['price'])

        # for Oanda, one order fill is a single trade.
        trade = TradeData(
            gateway_name=self.gateway_name,
            symbol=order.symbol,
            exchange=Exchange.OANDA,
            orderid=order_id,
            tradeid=order_id,
            direction=order.direction,
            offset=Offset.NONE,
            price=price,
            volume=order.volume,
            time=parse_time(data['time']),
        )
        self.gateway.on_trade(trade)

        # this message indicate that this order is full filled.
        # ps: oanda's order has only two state: NOTTRADED, ALLTRADED. It it my settings error?
        order.traded = order.volume
        order.status = Status.ALLTRADED
        # order.time = trade.time
        order.time = parse_time(data['time'])
        self.gateway.on_order(order)

    # quick references
    on_tick = on_price
    on_trade = on_order_filled