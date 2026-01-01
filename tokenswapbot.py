from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify
from web3 import Web3
from datetime import datetime, timedelta
import threading
import time

app = Flask(__name__)
app.secret_key = 'change_this_to_secret_key'

NETWORKS = {
    'base': {
        'rpc': 'https://mainnet.base.org',
        'chain_id': 8453,
        'router': '0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24',
        'weth': '0x4200000000000000000000000000000000000006',
        'explorer': 'https://basescan.org/tx/'
    },
    'ethereum': {
        'rpc': 'https://eth.llamarpc.com',
        'chain_id': 1,
        'router': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D',
        'weth': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2',
        'explorer': 'https://etherscan.io/tx/'
    }
}

ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactETHForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"}
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "WETH",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

TOKEN_ABI = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"}
]

scheduled_trades = []
completed_trades = []
trade_counter = 0
SLIPPAGE_DEFAULT = 5
lock = threading.Lock()

def add_scheduled_trade(trade):
    global trade_counter
    with lock:
        trade_counter += 1
        trade["id"] = trade_counter
        trade["status"] = "scheduled"
        trade["created_at"] = datetime.now()
        scheduled_trades.append(trade)
    return trade_counter

def remove_scheduled_trade(trade_id):
    global scheduled_trades
    with lock:
        scheduled_trades[:] = [t for t in scheduled_trades if t['id'] != trade_id]

def execute_swap(trade):
    trade_id = trade.get('id', 'manual')
    trade['status'] = 'executing'
    trade['execution_started'] = datetime.now()
    
    try:
        config = NETWORKS.get(trade['network'], NETWORKS['base'])
        w3 = Web3(Web3.HTTPProvider(config['rpc']))
        if not w3.is_connected():
            trade['status'] = 'failed'
            trade['error'] = 'RPC failed'
            return False

        account = w3.eth.account.from_key(trade['private_key'])
        router = w3.eth.contract(address=w3.to_checksum_address(config['router']), abi=ROUTER_ABI)
        token_addr = w3.to_checksum_address(trade['token_address'])

        if w3.eth.get_code(token_addr) == b'':
            trade['status'] = 'failed'
            trade['error'] = 'Invalid token'
            return False

        try:
            token = w3.eth.contract(address=token_addr, abi=TOKEN_ABI)
            symbol = token.functions.symbol().call()
            decimals = token.functions.decimals().call()
        except:
            symbol, decimals = "UNKNOWN", 18

        amount_in_wei = w3.to_wei(trade['eth_amount'], 'ether')
        path = [router.functions.WETH().call(), token_addr]
        try:
            amounts = router.functions.getAmountsOut(amount_in_wei, path).call()
            if len(amounts) < 2:
                trade['status'] = 'failed'
                trade['error'] = 'No liquidity'
                return False
            expected_out = amounts[-1]
        except Exception as e:
            trade['status'] = 'failed'
            trade['error'] = str(e)
            return False

        min_out = int(expected_out * (100 - trade.get('slippage', SLIPPAGE_DEFAULT)) / 100)

        deadline = int(time.time()) + 600
        nonce = w3.eth.get_transaction_count(account.address)
        gas_price = w3.eth.gas_price

        tx = router.functions.swapExactETHForTokens(
            min_out, path, account.address, deadline
        ).build_transaction({
            'from': account.address,
            'value': amount_in_wei,
            'gasPrice': gas_price,
            'nonce': nonce,
            'chainId': config['chain_id']
        })

        try:
            gas_estimate = w3.eth.estimate_gas(tx)
            gas_cost = gas_estimate * gas_price
            required = amount_in_wei + gas_cost + w3.to_wei(0.00005, 'ether')
        except:
            required = amount_in_wei + w3.to_wei(0.0005, 'ether')

        balance = w3.eth.get_balance(account.address)
        if balance < required:
            needed = w3.from_wei(required - balance, 'ether')
            trade['status'] = 'failed'
            trade['error'] = f'Need {needed:.6f} more ETH'
            return False

        tx['gas'] = int(gas_estimate * 1.3) if 'gas_estimate' in locals() else 300000

        signed = w3.eth.account.sign_transaction(tx, trade['private_key'])
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        trade['status'] = 'completed'
        trade['tx_hash'] = tx_hash.hex()
        trade['completed_at'] = datetime.now()
        trade['tokens_received'] = expected_out / (10 ** decimals)
        trade['symbol'] = symbol

        with lock:
            completed_trades.append(trade)
            if trade in scheduled_trades:
                scheduled_trades.remove(trade)

        return True

    except Exception as e:
        trade['status'] = 'failed'
        trade['error'] = str(e)
        with lock:
            completed_trades.append(trade)
            if trade in scheduled_trades:
                scheduled_trades.remove(trade)
        return False

def check_scheduled_trades():
    while True:
        now = datetime.now()
        for trade in scheduled_trades[:]:
            if trade['status'] == 'scheduled' and trade['schedule_time'] <= now:
                execute_swap(trade)
        time.sleep(5)

def estimate_trade(token_address, eth_amount, network='base', slippage=SLIPPAGE_DEFAULT):
    try:
        config = NETWORKS.get(network, NETWORKS['base'])
        w3 = Web3(Web3.HTTPProvider(config['rpc']))
        if not w3.is_connected():
            return {'error': 'RPC failed'}

        router = w3.eth.contract(address=w3.to_checksum_address(config['router']), abi=ROUTER_ABI)
        token_addr = w3.to_checksum_address(token_address)

        amount_in_wei = w3.to_wei(eth_amount, 'ether')
        path = [router.functions.WETH().call(), token_addr]

        amounts = router.functions.getAmountsOut(amount_in_wei, path).call()
        if len(amounts) < 2:
            return {'error': 'No liquidity'}

        expected_out = amounts[-1]

        token = w3.eth.contract(address=token_addr, abi=TOKEN_ABI)
        symbol = token.functions.symbol().call()
        decimals = token.functions.decimals().call()

        min_out = int(expected_out * (100 - slippage) / 100)

        return {
            'expected_out': expected_out / (10 ** decimals),
            'min_out': min_out / (10 ** decimals),
            'symbol': symbol
        }
    except Exception as e:
        return {'error': str(e)}

@app.route('/')
def index():
    return render_template_string(TEMPLATE_HTML, scheduled_trades=scheduled_trades, completed_trades=completed_trades,NETWORKS=NETWORKS)

@app.route('/estimate', methods=['POST'])
def estimate():
    data = request.json
    token = data.get('token')
    eth = float(data.get('eth', 0))
    slippage = int(data.get('slippage', SLIPPAGE_DEFAULT))
    network = data.get('network', 'base')
    result = estimate_trade(token, eth, network, slippage)
    return jsonify(result)

@app.route('/schedule_trade', methods=['POST'])
def schedule_trade():
    data = request.form
    token = data.get('token')
    eth = float(data.get('eth', 0))
    slippage = int(data.get('slippage', SLIPPAGE_DEFAULT))
    network = data.get('network', 'base')
    dt = data.get('datetime')
    private_key = data.get('private_key')
    save_key = 'save_key' in data

    if not token or eth <= 0 or not dt or not private_key:
        flash('Missing required fields', 'danger')
        return redirect(url_for('index'))

    try:
        schedule_time = datetime.strptime(dt, '%Y-%m-%d %H:%M:%S')
    except:
        flash('Invalid date/time format', 'danger')
        return redirect(url_for('index'))

    if schedule_time <= datetime.now():
        flash('Scheduled time must be in the future', 'danger')
        return redirect(url_for('index'))

    trade = {
        'token_address': token,
        'eth_amount': eth,
        'schedule_time': schedule_time,
        'network': network,
        'slippage': slippage,
        'private_key': private_key if save_key else '',
        'saved_key': save_key
    }

    add_scheduled_trade(trade)
    flash(f'Trade scheduled for {schedule_time}', 'success')
    return redirect(url_for('index'))

@app.route('/execute_trade', methods=['POST'])
def execute_trade():
    data = request.form
    token = data.get('token')
    eth = float(data.get('eth', 0))
    slippage = int(data.get('slippage', SLIPPAGE_DEFAULT))
    network = data.get('network', 'base')
    private_key = data.get('private_key')

    if not token or eth <= 0 or not private_key:
        flash('Missing required fields', 'danger')
        return redirect(url_for('index'))

    trade = {
        'token_address': token,
        'eth_amount': eth,
        'schedule_time': datetime.now(),
        'network': network,
        'slippage': slippage,
        'private_key': private_key
    }

    success = execute_swap(trade)
    if success:
        flash(f'Trade executed successfully with TX: {trade.get("tx_hash")}', 'success')
    else:
        flash(f'Trade failed: {trade.get("error")}', 'danger')

    return redirect(url_for('index'))

@app.route('/cancel_trade/<int:trade_id>')
def cancel_trade(trade_id):
    remove_scheduled_trade(trade_id)
    flash(f'Trade #{trade_id} cancelled', 'info')
    return redirect(url_for('index'))

TEMPLATE_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>UMK Bot</title>
<style>
body { font-family: Arial, sans-serif; margin: 20px; background:#f9fafb; }
.container { max-width: 900px; margin: auto; background: white; padding: 20px; border-radius:8px; box-shadow: 0 0 10px #ccc;}
h1 { text-align:center; }
form { margin-bottom: 20px; }
input[type=text], input[type=number], input[type=datetime-local], select { width: 100%; padding: 8px; margin: 4px 0 10px; box-sizing: border-box; }
button { padding: 10px 15px; background-color: #2b7a78; color: white; border: none; border-radius: 4px; cursor: pointer; }
button:hover { background-color: #17252a; }
table { width: 100%; border-collapse: collapse; margin-top: 20px; }
th, td { padding: 10px; border: 1px solid #ddd; text-align: left; }
th { background-color: #def2f1; }
.status-success { color: green; font-weight: bold; }
.status-failed { color: red; font-weight: bold; }
.status-scheduled { color: orange; font-weight: bold; }
.flash { padding: 10px; margin-bottom: 20px; }
.flash-success { background: #d4edda; color: #155724; }
.flash-danger { background: #f8d7da; color: #721c24; }
.flash-info { background: #cce5ff; color: #004085; }
</style>
<script>
async function estimateTrade() {
    const token = document.getElementById('token').value.trim();
    const eth = parseFloat(document.getElementById('eth').value);
    const slippage = parseInt(document.getElementById('slippage').value);
    const network = document.getElementById('network').value;
    if(!token || isNaN(eth) || eth <= 0) {
        document.getElementById('estimate').textContent = '';
        return;
    }
    const resp = await fetch('/estimate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token, eth, slippage, network})
    });
    const data = await resp.json();
    if(data.error) {
        document.getElementById('estimate').textContent = 'Error: ' + data.error;
    } else {
        document.getElementById('estimate').textContent = `You will get approx. ${data.expected_out.toFixed(6)} ${data.symbol} (min ${data.min_out.toFixed(6)}) for ${eth} ETH`;
    }
}
</script>
</head>
<body>
<div class="container">
<h1>UMK Bot - Micro Swap</h1>

{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    {% for category, message in messages %}
      <div class="flash flash-{{category}}">{{message}}</div>
    {% endfor %}
  {% endif %}
{% endwith %}

<form method="post" action="/execute_trade" oninput="estimateTrade()">
  <h2>Manual Swap</h2>
  <label>Network:
    <select name="network" id="network">
      <option value="base">Base</option>
      <option value="ethereum">Ethereum</option>
    </select>
  </label>
  <label>Token Address:
    <input type="text" name="token" id="token" placeholder="Token contract address" required />
  </label>
  <label>ETH Amount:
    <input type="number" id="eth" name="eth" step="0.0001" min="0.0001" required />
  </label>
  <label>Slippage %:
    <input type="number" id="slippage" name="slippage" value="5" min="0" max="50" />
  </label>
  <label>Private Key:
    <input type="text" name="private_key" required autocomplete="off" />
  </label>
  <div id="estimate" style="margin-bottom:12px;color:#555;"></div>
  <button type="submit">Execute Now</button>
</form>

<form method="post" action="/schedule_trade" oninput="estimateTrade()">
  <h2>Schedule Trade</h2>
  <label>Network:
    <select name="network" id="network-schedule">
      <option value="base">Base</option>
      <option value="ethereum">Ethereum</option>
    </select>
  </label>
  <label>Token Address:
    <input type="text" name="token" id="token-schedule" placeholder="Token contract address" required />
  </label>
  <label>ETH Amount:
    <input type="number" id="eth-schedule" name="eth" step="0.0001" min="0.0001" required />
  </label>
  <label>Slippage %:
    <input type="number" id="slippage-schedule" name="slippage" value="5" min="0" max="50" />
  </label>
  <label>Schedule Date/Time:
    <input type="text" id="datetime" name="datetime" placeholder="YYYY-MM-DD HH:MM:SS" required />
  </label>
  <label>Private Key:
    <input type="text" name="private_key" autocomplete="off" />
  </label>
  <label><input type="checkbox" name="save_key" /> Save private key for scheduled trade</label>
  <div id="estimate-schedule" style="margin-bottom:12px;color:#555;"></div>
  <button type="submit">Schedule Trade</button>
</form>

<h2>Scheduled Trades</h2>
<table>
  <thead>
    <tr><th>ID</th><th>Token</th><th>ETH</th><th>Status</th><th>Scheduled Time</th><th>TX Hash / Error</th><th>Cancel</th></tr>
  </thead>
  <tbody>
    {% for t in scheduled_trades %}
    <tr>
      <td>{{t.id}}</td>
      <td>{{t.token_address[-8:]}}</td>
      <td>{{'%.6f'|format(t.eth_amount)}}</td>
      <td class="status-scheduled">Scheduled</td>
      <td>{{t.schedule_time.strftime('%Y-%m-%d %H:%M:%S')}}</td>
      <td>{{t.error if t.status == 'failed' else ''}}</td>
      <td><a href="{{url_for('cancel_trade', trade_id=t.id)}}">Cancel</a></td>
    </tr>
    {% endfor %}
  </tbody>
</table>

<h2>Completed Trades</h2>
<table>
  <thead>
    <tr><th>ID</th><th>Token</th><th>ETH</th><th>Status</th><th>TX Hash</th><th>Received</th><th>Error</th></tr>
  </thead>
  <tbody>
    {% for t in completed_trades %}
    <tr>
      <td>{{t.id if t.id != 'manual' else '-'}}</td>
      <td>{{t.token_address[-8:]}}</td>
      <td>{{'%.6f'|format(t.eth_amount)}}</td>
      <td class="{{'status-success' if t.status == 'completed' else 'status-failed'}}">{{t.status.capitalize()}}</td>
      <td>
        {% if t.status == 'completed' %}
          <a href="{{NETWORKS[t.network].explorer + t.tx_hash}}" target="_blank">{{t.tx_hash[:10]}}...{{t.tx_hash[-6:]}}</a>
        {% else %}
          -
        {% endif %}
      </td>
      <td>
        {% if t.status == 'completed' %}
          {{'%.6f'|format(t.tokens_received) + ' ' + t.symbol}}
        {% else %}
          -
        {% endif %}
      </td>
      <td>{{t.error if t.status == 'failed' else '-'}}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>

<script>
document.getElementById('token').addEventListener('input', estimateTrade);
document.getElementById('eth').addEventListener('input', estimateTrade);
document.getElementById('slippage').addEventListener('input', estimateTrade);

document.getElementById('token-schedule').addEventListener('input', scheduleEstimate);
document.getElementById('eth-schedule').addEventListener('input', scheduleEstimate);
document.getElementById('slippage-schedule').addEventListener('input', scheduleEstimate);

async function estimateTrade() {
    const token = document.getElementById('token').value.trim();
    const eth = parseFloat(document.getElementById('eth').value);
    const slippage = parseInt(document.getElementById('slippage').value);
    const network = document.getElementById('network').value;
    if(!token || isNaN(eth) || eth <= 0) {
        document.getElementById('estimate').textContent = '';
        return;
    }
    const resp = await fetch('/estimate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token, eth, slippage, network})
    });
    const data = await resp.json();
    if(data.error) {
        document.getElementById('estimate').textContent = 'Error: ' + data.error;
    } else {
        document.getElementById('estimate').textContent = `You will get approx. ${data.expected_out.toFixed(6)} ${data.symbol} (min ${data.min_out.toFixed(6)}) for ${eth} ETH`;
    }
}

async function scheduleEstimate() {
    const token = document.getElementById('token-schedule').value.trim();
    const eth = parseFloat(document.getElementById('eth-schedule').value);
    const slippage = parseInt(document.getElementById('slippage-schedule').value);
    const network = document.getElementById('network-schedule').value;
    if(!token || isNaN(eth) || eth <= 0) {
        document.getElementById('estimate-schedule').textContent = '';
        return;
    }
    const resp = await fetch('/estimate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token, eth, slippage, network})
    });
    const data = await resp.json();
    if(data.error) {
        document.getElementById('estimate-schedule').textContent = 'Error: ' + data.error;
    } else {
        document.getElementById('estimate-schedule').textContent = `You will get approx. ${data.expected_out.toFixed(6)} ${data.symbol} (min ${data.min_out.toFixed(6)}) for ${eth} ETH`;
    }
}
</script>
</div>
</body>
</html>
'''

if __name__ == "__main__":
    threading.Thread(target=check_scheduled_trades, daemon=True).start()
    app.run(debug=True)
