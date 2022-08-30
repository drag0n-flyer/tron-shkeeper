import asyncio
import json
import logging
import traceback

import httpx
import websockets

from .config import config
from .db import save_event
from .logging import logger

FILTER = {}

def apply_filter(msg):
    cond = []

    try:
        cond = [
            msg['triggerName'] == 'contractEventTrigger',
            msg['eventName'] == 'Transfer',
            msg['topicMap']['to'] in FILTER,
        ]
    except Exception as e:
        # logger.exception(f"Exception while appling filter to {msg}:")
        pass
    return bool(cond and all(cond))

async def notify_shkeeper(symbol, txid):
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f'http://{config["SHKEEPER_HOST"]}/api/v1/walletnotify/{symbol}/{txid}',
            headers={'X-Shkeeper-Backend-Key': config['SHKEEPER_KEY']}
        )

async def ws_main():

    logger.debug('Waiting for filter....')
    while not FILTER:
        logger.debug(f'Still waiting for filter: {FILTER}')
        await asyncio.sleep(1)
    logger.info(f'Filter was set to: {FILTER}')

    logger.info(f"Connecting to the event server at ws://{config['EVENT_SERVER_HOST']} ...")
    async for websocket in websockets.connect(f"ws://{config['EVENT_SERVER_HOST']}"):
        logger.info('Connected.')
        try:
            async for message in websocket:
                try:
                    event = json.loads(message)
                    logger.debug(f'Received event: {event}')

                    if apply_filter(event):
                        save_event(event['transactionId'], message)
                        symbol = FILTER[event['topicMap']['to']]
                        await notify_shkeeper(symbol, event['transactionId'])

                except Exception as e:
                    logger.exception(f"Message processing exception: {message} {traceback.format_exc()}")
                    continue
        except websockets.ConnectionClosed:
            logger.info('Server closed the connection, reconneting.')
        except Exception as e:
            logger.exception(f"Exception in event listener")

def events_listener():
    asyncio.run(ws_main())
