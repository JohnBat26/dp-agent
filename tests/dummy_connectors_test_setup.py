import asyncio
import argparse
import uuid

from aiohttp import web
from datetime import datetime
from string import hexdigits
from random import choice

from core.agent import Agent
from core.pipeline import Pipeline, Service, simple_workflow_formatter
from core.connectors import HttpOutputConnector
from core.config_parser import parse_old_config
from core.state_manager import StateManager
from core.transform_config import DEBUG
from core.run import prepare_agent

parser = argparse.ArgumentParser()
parser.add_argument('-p', '--port', help='port for http client, default 4242', default=4242)
args = parser.parse_args()
CHANNEL = 'vk'


class DummyConnector:
    def __init__(self, returns, sleeptime, service_name):
        self.returns = returns
        self.sleeptime = sleeptime
        self.service_name = service_name

    async def send(self, payload):
        await asyncio.sleep(self.sleeptime)
        return {self.service_name: {"text": choice(self.returns), "confidence": 0.5}}


class DummySelectorConnector:
    def __init__(self, returns, sleeptime, service_name):
        self.returns = returns
        self.sleeptime = sleeptime
        self.service_name = service_name

    async def send(self, payload):
        await asyncio.sleep(self.sleeptime)
        return {self.service_name: self.returns}


async def on_shutdown(app):
    await app['client_session'].close()


async def init_app(register_msg, intermediate_storage, on_startup, on_shutdown_func=on_shutdown):
    app = web.Application(debug=True)
    handle_func = await api_message_processor(register_msg, intermediate_storage)
    app.router.add_post('/', handle_func)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown_func)
    return app


def prepare_startup(consumers, process_callable, session):
    result = []
    for i in consumers:
        result.append(asyncio.ensure_future(i.call_service(process_callable)))

    async def startup_background_tasks(app):
        app['consumers'] = result
        app['client_session'] = session

    return startup_background_tasks


async def api_message_processor(register_msg, intermediate_storage):
    async def api_handle(request):
        user_id = None
        bot_response = None
        if request.method == 'POST':
            if request.headers.get('content-type') != 'application/json':
                raise web.HTTPBadRequest(reason='Content-Type should be application/json')
            data = await request.json()
            user_id = data.get('user_id')
            payload = data.get('payload', '')

            if not user_id:
                raise web.HTTPBadRequest(reason='user_id key is required')

            event = asyncio.Event()
            message_uuid = uuid.uuid4().hex
            await register_msg(utterance=payload, user_telegram_id=user_id, user_device_type='http',
                               date_time=datetime.now(), location='', channel_type=CHANNEL,
                               event=event, message_uuid=message_uuid)
            await event.wait()
            bot_response = intermediate_storage.pop(message_uuid)

            if bot_response is None:
                raise RuntimeError('Got None instead of a bot response.')

        return web.json_response({'user_id': user_id, 'response': bot_response})

    return api_handle


def main():
    services, workers, session = parse_old_config()

    for s in services:
        if 'RESPONSE_SELECTORS' in s.tags:
            continue
        if s.is_selector():
            s.connector_func = DummySelectorConnector(['chitchat', 'odqa'], 0.01, s.name).send
        else:
            s.connector_func = DummyConnector(['we have a phrase', 'and another one', 'not so short one'], 0.01, s.name).send
    intermediate_storage = {}
    endpoint = Service('http_responder', HttpOutputConnector(intermediate_storage).send,
                       StateManager.save_dialog_dict, 1, ['responder'])
    input = Service('input', None, StateManager.add_human_utterance_simple_dict, 1, ['input'])
    register_msg, process_callable = prepare_agent(services, endpoint, input)
    app = init_app(register_msg, intermediate_storage, prepare_startup(workers, process_callable, session),
                   on_shutdown)

    web.run_app(app, port=args.port)


if __name__ == '__main__':
    main()
