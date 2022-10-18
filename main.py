import asyncio
from asyncio import constants
from cmath import exp
import logging
import os
from time import time
import pandas as pd
import regex as re

import jinja2
import aiohttp_jinja2
import aiohttp as http
from aiohttp import web

from web3 import Web3
from web3.contract import Contract
from web3.middleware import geth_poa_middleware

import json
from dotenv import load_dotenv

load_dotenv()



app = web.Application()
routes = web.RouteTableDef()

aiohttp_jinja2.setup(
    app, loader=jinja2.FileSystemLoader(os.path.join(os.getcwd(), "templates"))
)




# personal wallet constants
WALLET_PUBLIC_KEY = os.getenv('WALLET_PUBLIC')
WALLET_PRIVATE_KEY = os.getenv('WALLET_PRIVATE')


# bsc blockchain related constants
BSC_ADDRESS = 'https://bsc-dataseed.binance.org/'

ABI_MOMO_BID = [{"inputs": [
                    {"internalType": "address", "name": "auctor_", "type": "address"},
                    {"internalType": "uint256", "name": "index_", "type": "uint256"},
                    {"internalType": "uint256","name": "startTime_","type": "uint256"},
                    {"internalType": "uint256","name": "price_","type": "uint256"}
            ], "name": "bid", "outputs": [], "stateMutability": "payable", "type": "function"}]

ABI_GEM_BID = [{"inputs": [
                    {"internalType": "address", "name": "_platform", "type": "address"},
                    {"internalType": "uint256", "name": "_index", "type": "uint256"},
                    {"internalType": "uint256","name": "price","type": "uint256"},
            ], "name": "bid", "outputs": [], "stateMutability": "payable", "type": "function"}]


MOMO_CONTRACT_ADDRESS = '0xcb0cffc2b12739d4be791b8af7fbf49bc1d6a8c2'
GEM_CONTRACT_ADDRESS = '0x819e97c7da2c784403b790121304db9e6a038de9'

# connect to bsc chain
w3 = Web3(Web3.HTTPProvider(BSC_ADDRESS))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)
logging.info(f'Connected to BSC: {w3.isConnected()}')



# data management constants
DATA_MOMOS_PATH = 'data/momos.json'
DATA_FILTERS_PATH = 'data/filters.json'
DATA_BOUGHTS_PATH = 'data/bought.json'
DATA_GEMS_PATH = 'data/gems.json'

def open_dict(path : str) -> dict:
    try:
        with open(path) as file:
            return json.loads(file.read())
    except:
        return {}

def persist_dict(path : str, data : dict) -> dict:
    try:
        with open(path, 'w') as file:
            file.write(json.dumps(data))
    except:
        logging.warning(f'Failed persisting data into {path}')


data_momos = open_dict(DATA_MOMOS_PATH)
data_gems = open_dict(DATA_GEMS_PATH)
data_filters = open_dict(DATA_FILTERS_PATH)
data_boughts = open_dict(DATA_BOUGHTS_PATH)


# further constants
bot_running = False
html_filter_input = 'momo'
current_nonce = 0
nonce_lock = asyncio.Lock()

task_momos_watcher : asyncio.Task = None
task_momos_data_updater : asyncio.Task = None


momo_qualities = {
    -1 : 'None',
    1: 'Common',
    2: 'Uncommon',
    3: 'Unique',
    4: 'Rare',
    5: 'Epic',
    6: 'Legendary',
}

gem_levels = {
    1 : 'Lvl. 1',
    2 : 'Lvl. 2',
    3 : 'Lvl. 3',
    4 : 'Lvl. 4',
    5 : 'Lvl. 5',
    6 : 'Lvl. 6',
    7 : 'Lvl. 7',
    8 : 'Lvl. 8',
    9 : 'Lvl. 9',
    10 : 'Lvl. 10',
}

gem_name = {
    100 : 'Ruby',
    200 : 'Emerald',
    300 : 'Sapphire',
    400 : 'Topaz',
}

gem_color = {
    100 : '#d00b00',
    200 : '#4eb403',
    300 : '#205acf',
    400 : '#ffee41',
}


# method watch available items in mobox
async def data_updater():

    global data_momos, data_gems
    
    async with http.ClientSession() as session:
        
        while True:
            url = 'https://www.mobox.io/momo/js/app.1f433f44.js'
            async with session.get(url) as resp:
                
                data = await resp.text()

                momos_data = re.findall('{prototype:([0-9]+?),tokenName:\"Name_(.*?)\",quality:([0-9]+?),category:([0-9]+?),mmNum:([0-9]+?),cnName:\".*?\"}', data)
                gem_data = re.findall('\{id:([0-9]+?),num:.*?,productivityRate:.*?\}', data, flags=re.DOTALL)
                names = re.findall('\"Name_([0-9]*?)\":\"([A-Za-z0-9À-Ÿ\-\'\.\\\ ]*?)\"', data, flags=re.DOTALL)

                gem_ids = list(map(int, set(gem_data)))

                names = dict(list(set(names)))
                data_momos = { momo[0] : {
                    'tokenName' : momo[1],
                    'quality' : momo[2],
                    'category' : momo[3],
                    'mmNum' : momo[4],
                    'name' : names[momo[1]]
                } for momo in momos_data }

                data_gems = { id : {
                    'level' : gem_levels[id % 100],
                    'name': gem_name[id - (id%100)],
                    'color': gem_color[id - (id%100)]
                    } for id in gem_ids }

            persist_dict(DATA_MOMOS_PATH, data_momos)
            persist_dict(DATA_GEMS_PATH, data_gems)

            await asyncio.sleep(3600)

            # except Exception as err:
            #     logging.warning(f'Failed performing momo watcher task. {str(err)}')



def mobox_price_add_amount(price, add):
    return price + 1000000000 * add

def mobox_price_to_contract_price(price):
    return int(price * 1000000000)

# set bid to smart contract
async def set_bid(data : dict, ismomo=True):


        global current_nonce

        try:

            start_time = data['uptime']
            id = data['id'] if ismomo else data['orderId']
            id = f'{id}_{start_time}'            

            data_boughts[id] = {'hash' : '/', 'data' : data, 'status' : 'pending'}

            # set price
            price = data['nowPrice'] if ismomo else data['price']
            price = mobox_price_add_amount(price, 0.0001)
            price = mobox_price_to_contract_price(price)

            # set from/to adresses
            auctor_address = data['auctor']
            contract_address = MOMO_CONTRACT_ADDRESS if ismomo else GEM_CONTRACT_ADDRESS
            _contract_address = w3.toChecksumAddress(contract_address)
            _auctor_address = w3.toChecksumAddress(auctor_address)

            # prepare transaction
            abi = ABI_MOMO_BID if ismomo else ABI_GEM_BID
            contract : Contract = w3.eth.contract(address=_contract_address, abi=abi)


            index = data['index'] if ismomo else data['orderId']
            if ismomo:
                transaction = contract.functions.bid(_auctor_address, index, start_time, price)
            else:
                transaction = contract.functions.bid(_auctor_address, index, price)
            
            async with nonce_lock:
                nonce = w3.eth.get_transaction_count(WALLET_PUBLIC_KEY)
                if nonce > current_nonce:
                    current_nonce = nonce
                else:
                    nonce = current_nonce + 1
                    current_nonce = nonce

            transaction = transaction.buildTransaction({
                'chainId': 56,
                'gas': 800000,
                'gasPrice': w3.toWei('15', 'gwei'),
                'nonce': nonce
            })

            signed = w3.eth.account.sign_transaction(transaction, WALLET_PRIVATE_KEY)


            # if momo wait 2 minutes
            if ismomo:
                now = int(time())
                till = start_time + 114
                await asyncio.sleep(till - now)

            else:
                await asyncio.sleep(0)

            # send transaction
            # tx_hash = w3.eth.sendRawTransaction(signed.rawTransaction)
            logging.warning(f'executed bid at {int(time())}')

            # data_boughts[id]['hash'] = tx_hash.hex()
            data_boughts[id]['status'] = 'success'
            persist_dict(DATA_BOUGHTS_PATH, data_boughts)

        except Exception as err:

            data_boughts[id] = {'hash' : 'error', 'data' : data, 'status' : 'error', 'msg' : str(err)}
            persist_dict(DATA_BOUGHTS_PATH, data_boughts)



# crawl momos and gems
async def search_in_marketplace() -> pd.DataFrame:

    global bot_running, data_filters, data_boughts

    def filter_momo_condition(row):

        prototype = int(row['prototype'])
        price = int(row['nowPrice'])
        hashrate =  int(row['lvHashrate'])

        start_time = row['uptime']
        id = row['id']
        id = f'{id}_{start_time}' 

        if id in data_boughts:
            return False

        for _, filter in data_filters.items():
            if (filter['type'] != 'gem'):
                if (not (filter['type'] == 'momo') or prototype == int(filter['key'])):
                    if (not (filter['type'] == 'quality') or (str(prototype) in data_momos and data_momos[str(prototype)]['quality'] == str(filter['key']))):
                        if (not (filter['filterby'] == 'price') or (price < float(filter['value']) * 1000000000)):
                            if (not (filter['filterby'] == 'hash') or ((price / 1000000000 / hashrate) < float(filter['value']))):
                                return True
        return False

    def filter_gem_condition(row):

        ids = row['ids']
        amounts = row['amounts']
        price = int(row['price'])

        if len(ids) != 1 or len(amounts) != 1:
            return False

        start_time = row['uptime']
        id = row['orderId']
        id = f'{id}_{start_time}'   

        if id in data_boughts:
            return False

        for _, filter in data_filters.items():
            if (filter['type'] == 'gem'):
                if (int(ids[0]) == int(filter['key'])):
                    if (filter['filterby'] == 'price' and (price / int(amounts[0])) < float(filter['value']) * 1000000000):
                        return True

        return False


    async with http.ClientSession() as session:
        while bot_running:
            logging.warning('iter')
            start = int(time())
            async with session.get('https://nftapi.mobox.io/auction/search/BNB?page=1&limit=10000') as resp:
                data = await resp.json()
                df = pd.DataFrame(data['list'])
                df = df[df.apply(filter_momo_condition, axis=1)]
                if not df.empty:
                    for _, momo in df.iterrows():
                        logging.warning('set momo bid')
                        asyncio.create_task(set_bid(momo.to_dict()))

            async with session.get('https://nftapi.mobox.io/gem_auction/search/BNB?page=1&limit=1000') as resp:
                data = await resp.json()
                df = pd.DataFrame(data['list'])
                df = df[df.apply(filter_gem_condition, axis=1)]
                if not df.empty:
                    for _, gem in df.iterrows():
                        logging.warning('set gem bid')
                        asyncio.create_task(set_bid(gem.to_dict(), ismomo=False))

            now = int(time())
            await asyncio.sleep((start + 5) - now)


async def start():

    global bot_running, task_momos_watcher, task_momos_data_updater

    bot_running = True
    loop = asyncio.get_event_loop()
    task_momos_watcher = loop.create_task(search_in_marketplace())
    task_momos_data_updater = loop.create_task(data_updater())


@routes.get('/')
@aiohttp_jinja2.template('index.html')
async def index(request: web.Request):
   context = {
    'data': {
        'momo' : data_momos,
        'gem' : data_gems,
        'filter' : data_filters,
        'bought' : data_boughts,
        'quality': momo_qualities,
        'running' : bot_running
    },
   }
   return context

@routes.post('/filter')
async def add_filter(request: web.Request):
    data = await request.post()
    
    type = data.get('type', str)
    key = data.get(type, str)
    filterby = data.get('filterby', str)
    value = data.get(filterby, str)

    if not (type == 'gem' and filterby == 'hash'):
        ids = [int(k) for k,_ in data_filters.items()]
        ids = ids if len(ids) else [-1]
        data_filters[str(max(ids) + 1)] = {
            'type' : type,
            'key' : key,
            'filterby' : filterby,
            'value' : value
        }
        persist_dict(DATA_FILTERS_PATH, data_filters)
        return web.json_response({'msg' : 'added'})

    return web.json_response({'msg' : 'wrong input'})

@routes.post('/filter/{id}')
async def delete_filter(request: web.Request):
    id = request.match_info.get('id', None)
    del data_filters[id]
    persist_dict(DATA_FILTERS_PATH, data_filters)

    return web.json_response({'msg' : 'deleted'})

@routes.post('/start')
async def start_bot(request: web.Request):

    global bot_running
    if not bot_running:
        bot_running = True
        loop = asyncio.get_event_loop()
        loop.create_task(start())

    return web.json_response({'msg' : 'started'})


@routes.post('/stop')
async def stop_bot(request: web.Request):
    global bot_running

    if bot_running:
        task_momos_watcher.cancel()
        task_momos_data_updater.cancel()

    bot_running = False

    return web.json_response({'msg' : 'stopped'})

if __name__ == '__main__':
    app.add_routes(routes)
    web.run_app(app, port=1212)