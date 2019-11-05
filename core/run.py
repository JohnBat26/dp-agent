import argparse
import asyncio
import logging
from datetime import datetime
from os import getenv
from typing import Optional

from aiogram import Bot
from aiogram.dispatcher import Dispatcher
from aiogram.utils import executor
from aiohttp import ClientSession, web

from core.agent import Agent
from core.config_parser import get_service_gateway_config, parse_old_config
from core.connectors import EventSetOutputConnector
from core.db import db
from core.http_api import init_app
from core.log import ResponseLogger
from core.pipeline import Pipeline
from core.service import Service
from core.state_manager import StateManager

parser = argparse.ArgumentParser()
parser.add_argument('-m', '--mode', help='run agent in default mode or as one of the high load components',
                    default='default', choices=['default', 'agent', 'service', 'channel'])
parser.add_argument('-n', '--service-name', help='service name for service run mode', type=str)
parser.add_argument('-ch', '--channel', help='run agent in telegram, cmd_client or http_client', type=str,
                    choices=['cmd_client', 'http_client', 'telegram'], default='cmd_client')
parser.add_argument('-p', '--port', help='port for http client, default 4242', default=4242)
parser.add_argument('-d', '--debug', help='run in debug mode', action='store_true')
parser.add_argument('-ls', '--log-scope', help='set services response timeouts log scope', type=str, default=None,
                    choices=['agent', 'service', 'both'])

args = parser.parse_args()
MODE = args.mode
CHANNEL = args.channel


def prepare_agent(services, state_manager, endpoint: Service, input_serv: Service, log_scope: Optional[str]):
    pipeline = Pipeline(services)
    pipeline.add_responder_service(endpoint)
    pipeline.add_input_service(input_serv)
    if log_scope is not None:
        response_logger_callable = ResponseLogger(log_scope)
    else:
        response_logger_callable = None
    agent = Agent(pipeline, state_manager, response_logger_callable=response_logger_callable)
    return agent.register_msg, agent.process, agent


async def run(register_msg):
    user_id = input('Provide user id: ')
    while True:
        msg = input(f'You ({user_id}): ').strip()
        if msg:
            response = await register_msg(utterance=msg, user_telegram_id=user_id, user_device_type='cmd',
                                          location='lab', channel_type=CHANNEL,
                                          deadline_timestamp=None, require_response=True)
            print('Bot: ', response['dialog'].utterances[-1].text)


class TelegramMessageProcessor:
    def __init__(self, register_msg):
        self.register_msg = register_msg

    async def handle_message(self, message):
        response = await self.register_msg(
            utterance=message.text,
            user_telegram_id=str(message.from_user.id),
            user_device_type='telegram',
            date_time=datetime.now(), location='', channel_type='telegram',
            require_response=True
        )
        await message.answer(response['dialog']['utterances'][-1]['text'])


def run_default():
    sm = StateManager(db)
    services, workers, session, gateway = parse_old_config(sm)

    if CHANNEL == 'cmd_client':
        endpoint = Service('cmd_responder', EventSetOutputConnector('cmd_responder').send,
                           sm.save_dialog, 1, ['responder'])
        input_srv = Service('input', None, sm.add_human_utterance, 1, ['input'])
        loop = asyncio.get_event_loop()
        loop.set_debug(args.debug)
        register_msg, process, _ = prepare_agent(services, sm,  endpoint, input_srv, log_scope=args.log_scope)
        if gateway:
            gateway.on_channel_callback = register_msg
            gateway.on_service_callback = process
        future = asyncio.ensure_future(run(register_msg))
        for i in workers:
            loop.create_task(i.call_service(process))
        try:
            loop.run_until_complete(future)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            raise e
        finally:
            future.cancel()
            if session:
                loop.run_until_complete(session.close())
            if gateway:
                gateway.disconnect()
            loop.stop()
            loop.close()
            logging.shutdown()
    elif CHANNEL == 'http_client':
        if not session:
            session = ClientSession()
        endpoint = Service('http_responder', EventSetOutputConnector('http_responder').send,
                           sm.save_dialog, 1, ['responder'])
        input_srv = Service('input', None, sm.add_human_utterance, 1, ['input'])
        register_msg, process_callable, agent = prepare_agent(services, sm, endpoint, input_srv, args.log_scope)
        if gateway:
            gateway.on_channel_callback = register_msg
            gateway.on_service_callback = process_callable
        app = init_app(agent, session, workers, args.debug)
        web.run_app(app, port=args.port)

    elif CHANNEL == 'telegram':
        token = getenv('TELEGRAM_TOKEN')
        proxy = getenv('TELEGRAM_PROXY')

        loop = asyncio.get_event_loop()

        bot = Bot(token=token, loop=loop, proxy=proxy)
        dp = Dispatcher(bot)
        endpoint = Service('telegram_responder', EventSetOutputConnector('telegram_responder').send,
                           StateManager.save_dialog, 1, ['responder'])
        input_srv = Service('input', None, StateManager.add_human_utterance, 1, ['input'])
        register_msg, process, _ = prepare_agent(
            services, sm, endpoint, input_srv, log_scope=args.log_scope)
        if gateway:
            gateway.on_channel_callback = register_msg
            gateway.on_service_callback = process
        for i in workers:
            loop.create_task(i.call_service(process))
        tg_msg_processor = TelegramMessageProcessor(register_msg)

        dp.message_handler()(tg_msg_processor.handle_message)

        executor.start_polling(dp, skip_updates=True)


def run_agent():
    raise NotImplementedError


def run_service():
    from core.transport.mapping import GATEWAYS_MAP, CONNECTORS_MAP

    service_name = args.service_name
    gateway_config = get_service_gateway_config(service_name)
    service_config = gateway_config['service']

    formatter = service_config['formatter']
    connector_type = service_config['protocol']
    connector_cls = CONNECTORS_MAP[connector_type]
    connector = connector_cls(service_config=service_config, formatter=formatter)

    transport_type = gateway_config['transport']['type']
    gateway_cls = GATEWAYS_MAP[transport_type]['service']
    gateway = gateway_cls(config=gateway_config, to_service_callback=connector.send_to_service)

    loop = asyncio.get_event_loop()

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        raise e
    finally:
        gateway.disconnect()
        loop.stop()
        loop.close()
        logging.shutdown()


def run_channel():
    raise NotImplementedError


def main():
    if MODE == 'default':
        run_default()
    elif MODE == 'agent':
        run_agent()
    elif MODE == 'service':
        run_service()
    elif MODE == 'channel':
        run_channel()


if __name__ == '__main__':
    main()
