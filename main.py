import asyncio
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


address = os.getenv('WALLET_PUBLIC')
private = os.getenv('WALLET_PRIVATE')

bsc = 'https://bsc-dataseed.binance.org/'

#############################################################
## connect to BSC chain
#############################################################
w3 = Web3(Web3.HTTPProvider(bsc))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)
print('Connected to BSC:', w3.isConnected())



#############################################################
## open data files
#############################################################
# open momos
try:
    with open('momos.json') as f:
        momos = json.loads(f.read())
except:
    momos = {}

# open filters
try:
    with open('filters.json') as f:
        filters = json.loads(f.read())
except:
    filters = {}

# open bought momos
try:
    with open('bought.json') as file:
            bought = json.load(file)
except:
    bought = {}

start_flag = False
searcher : asyncio.Task = None
updater : asyncio.Task = None


qualities = {
    -1 : 'None',
    1: 'Common',
    2: 'Uncommon',
    3: 'Unique',
    4: 'Rare',
    5: 'Epic',
    6: 'Legendary',
}

#############################################################
## set bid to smart contract for momo
#############################################################   
async def cron_fetch_momo_list():

    async with http.ClientSession() as session:
        while True:
            async with session.get('https://www.mobox.io/momo/js/app.1f433f44.js') as resp:
                data = await resp.text()
                momos_data = re.findall('{prototype:([0-9]+?),tokenName:\"Name_(.*?)\",quality:([0-9]+?),category:([0-9]+?),mmNum:([0-9]+?),cnName:\".*?\"}', data)
                names = re.findall('\"Name_([0-9]*?)\":\"([A-Za-z0-9À-Ÿ\-\'\.\\\ ]*?)\"', data, flags=re.DOTALL)

                names = dict(list(set(names)))
                momos = { momo[0] : {
                    'tokenName' : momo[1],
                    'quality' : momo[2],
                    'category' : momo[3],
                    'mmNum' : momo[4],
                    'name' : names[momo[1]]
                } for momo in momos_data }

            with open('momos.json', 'w') as file:
                file.write(json.dumps(momos)) 
                
            await asyncio.sleep(3600)


#############################################################
## set bid to smart contract for momo
#############################################################   
async def set_bid(contract_address: str, momo):

        auctor_address = momo['auctor']
        id = momo['id']
        index = momo['index']
        start_time = momo['uptime']
        price = momo['nowPrice']

        bought[f'{id}_{start_time}'] = {'hash' : 'pending, waiting till listed', 'data' : momo.to_dict()}

        price = price / 1000000000
        price = price + 0.0001
        price = int(price * 1000000000 * 1000000000)

        _contract_address = w3.toChecksumAddress(contract_address)
        _auctor_address = w3.toChecksumAddress(auctor_address)

        abi = [{"inputs": [
                {"internalType": "address", "name": "auctor_", "type": "address"},
                {"internalType": "uint256", "name": "index_", "type": "uint256"},
                {"internalType": "uint256","name": "startTime_","type": "uint256"},
                {"internalType": "uint256","name": "price_","type": "uint256"}
        ], "name": "bid", "outputs": [], "stateMutability": "payable", "type": "function"}]

        contract : Contract = w3.eth.contract(address=_contract_address, abi=abi)
        transaction = contract.functions.bid( _auctor_address, index, start_time, price)
        transaction = transaction.buildTransaction({
            'chainId': 56,
            'gas': 800000, #TODO: adapt gas
            'gasPrice': w3.toWei('15', 'gwei'),
            'nonce': w3.eth.get_transaction_count(address),
        })

        signed = w3.eth.account.sign_transaction(transaction, private)
        
        now = int(time())
        till = start_time + 114
        await asyncio.sleep(till - now)
        tx_hash = w3.eth.sendRawTransaction(signed.rawTransaction)
        logging.warning(f'executed bid at {int(time())}')

        bought[f'{id}_{start_time}'] = {'hash' : tx_hash.hex(), 'data' : momo.to_dict()}
        with open('bought.json', 'w') as file:
            json.dump(bought, file)


#############################################################
## crawl Momos
#############################################################
async def search_momos_in_marketplace() -> pd.DataFrame:

    global start_flag, filters, bought

    def filter_condition(row):

        prototype = int(row['prototype'])
        price = int(row['nowPrice'])
        hashrate =  int(row['lvHashrate'])
        id = row['id']
        start_time = row['uptime']

        if f'{id}_{start_time}' in bought:
            logging.warning(f'already bought {id}_{start_time}')
            return False

        for _, filter in filters.items():
            if (prototype == filter['momo'] or filter['momo'] == -1):
                if ((str(prototype) in momos and momos[str(prototype)]['quality'] == str(filter['quality'])) or filter['quality'] == -1):
                    if ((price < filter['price'] * 1000000000) or filter['price'] == -1):
                        if (((price / 1000000000 / hashrate) < filter['hash']) or filter['hash'] == -1):
                            return True
        return False

    async with http.ClientSession() as session:
        while start_flag:
            async with session.get('https://nftapi.mobox.io/auction/search/BNB?page=1&limit=10000&category=&vType=&sort=-time&pType=') as resp:
                data = await resp.json()
                df = pd.DataFrame(data['list'])
                filtered = df.apply(filter_condition, axis=1)
                df = df[filtered]
                if not df.empty:
                    for _, momo in df.iterrows():
                        logging.warning('set bid')
                        asyncio.create_task(set_bid('0xcb0cffc2b12739d4be791b8af7fbf49bc1d6a8c2', momo))

            await asyncio.sleep(5)
            logging.warning('iter')


async def start():

    global start_flag, searcher, updater

    start_flag = True
    loop = asyncio.get_event_loop()
    searcher = loop.create_task(search_momos_in_marketplace())
    updater = loop.create_task(cron_fetch_momo_list())


@routes.get('/')
@aiohttp_jinja2.template('index.html')
async def index(request: web.Request):
   context = {
    'momos' : momos,
    'filters' : filters,
    'bought' : bought,
    'qualities': qualities,
    'running' : start_flag
   }
   return context

@routes.post('/filter')
async def add_filter(request: web.Request):
    data = await request.post()

    quality = data.get('quality', str)
    quality = -1 if quality == '' else int(quality)
    momo = data.get('momo', str)
    momo = -1 if momo == '' else int(momo)
    price = data.get('price', str)
    price = -1 if price == '' else float(price)
    hash = data.get('hash', str)
    hash = -1 if hash == '' else float(hash)

    quality_exist = quality in qualities.keys()
    momo_exist = str(momo) in momos.keys() or momo == -1
    least_one_key = bool(momo == -1) != bool(quality == -1)
    least_one_val = bool(price == -1) != bool(hash == -1)
    if (quality_exist and momo_exist and least_one_key and least_one_val):
        ids = [int(k) for k,_ in filters.items()]
        ids = ids if len(ids) else [-1]
        filters[str(max(ids) + 1)] = {
            'quality' : quality,
            'momo' : momo,
            'price' : price,
            'hash' : hash
        }
        with open('filters.json', 'w') as file:
            file.write(json.dumps(filters))

    return web.json_response({'msg' : 'added'})

@routes.post('/filter/{id}')
async def delete_filter(request: web.Request):
    id = request.match_info.get('id', None)
    del filters[id]
    with open('filters.json', 'w') as file:
        file.write(json.dumps(filters))
    
    return web.json_response({'msg' : 'deleted'})

@routes.post('/start')
async def start_bot(request: web.Request):

    global start_flag
    if not start_flag:
        start_flag = True
        loop = asyncio.get_event_loop()
        loop.create_task(start())

    return web.json_response({'msg' : 'started'})


@routes.post('/stop')
async def stop_bot(request: web.Request):
    global start_flag

    if start_flag:
        searcher.cancel()
        updater.cancel()

    start_flag = False

    return web.json_response({'msg' : 'stopped'})

if __name__ == '__main__':
    app.add_routes(routes)
    web.run_app(app, port=1212)