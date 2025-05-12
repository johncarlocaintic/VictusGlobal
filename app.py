from flask import Flask, request, jsonify
import requests
import re
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import time
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import json
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import threading
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Get environment variables with fallbacks
CMC_API_KEY = os.getenv('CMC_API_KEY', 'fa253a05-6e6d-4993-8f69-a8e3ad522a49')
CMC_INFO_URL = 'https://pro-api.coinmarketcap.com/v2/cryptocurrency/info'
CMC_MAP_URL = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/map'
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '7678977006:AAEOLzVop7uhMLACStxxn0IOXGnI6iiP5Pg')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '8012302240')
TELEGRAM_API_URL = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'

CMC_URL_REGEX = re.compile(r'https?://coinmarketcap\.com/currencies/[^/]+/?')

# Add these global variables
last_processed_message_id_per_chat = {}
last_message_time_per_chat = {}
spam_attempts_per_chat = {}  # Track spam attempts per chat
BLOCK_DURATION = 10  # Block duration in seconds
COOLDOWN_SECONDS = 2  # Cooldown period in seconds
chat_locks = {}

def format_dollar(amount):
    if amount is None:
        return None
    return "${:,.0f}".format(amount)

def get_id_from_slug(slug):
    headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY}
    params = {'symbol': slug.upper()}
    response = requests.get(CMC_INFO_URL, headers=headers, params=params)
    data = response.json()
    print("CMC INFO API response:", data)
    if 'data' in data and data['data']:
        # data['data'] is a dict keyed by symbol, e.g. 'XMR'
        for symbol, info in data['data'].items():
            if info and 'id' in info:
                return info['id']
    print("Error from CMC API:", data)
    return None

def parse_volume(volume_str):
    try:
        return float(volume_str.replace('$', '').replace(',', '').replace('*', '').replace('**', '').strip())
    except:
        return 0

def highlight_element(driver, element, color='red', background='yellow'):
    from flask import current_app as app
    app.logger.info(f"Highlighting element: {element}")
    driver.execute_script("arguments[0].style.border='3px solid %s'; arguments[0].style.background='%s';" % (color, background), element)

def get_chrome_options():
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--disable-extensions')
    chrome_options.add_argument('--disable-infobars')
    chrome_options.add_argument('--disable-notifications')
    chrome_options.add_argument('--disable-popup-blocking')
    chrome_options.add_argument('--disable-blink-features=AutomationControlled')
    chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    return chrome_options

def get_webdriver():
    try:
        # Try to use ChromeDriverManager first
        service = Service(ChromeDriverManager().install())
    except:
        # Fallback to system Chrome
        service = Service('/usr/bin/chromedriver')
    
    return webdriver.Chrome(service=service, options=get_chrome_options())

def get_top_dex_market_selenium(slug):
    url = f"https://coinmarketcap.com/currencies/{slug}/markets/"
    driver = get_webdriver()
    driver.get(url)
    time.sleep(5)

    try:
        consent_button = driver.find_element(By.XPATH, "//button[contains(., 'Accept')]")
        consent_button.click()
        time.sleep(1)
    except:
        pass

    # Wait for the DEX tab to be present and click it if needed
    try:
        dex_tab = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "li[data-test='dex']"))
        )
        if "Tab_selected__zLjtL" not in dex_tab.get_attribute("class"):
            driver.execute_script("arguments[0].scrollIntoView(true);", dex_tab)
            dex_tab.click()
            time.sleep(2)
    except Exception as e:
        print("DEX tab not found or could not be clicked:", e)

    # Wait for the table to be present after clicking
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.TAG_NAME, "table"))
    )

    try:
        table = driver.find_element(By.TAG_NAME, "table")
        headers = table.find_elements(By.TAG_NAME, "th")
        header_texts = [h.text.strip().lower() for h in headers]
        liquidity_idx = header_texts.index("liquidity score")

        # Find all DEX rows
        rows = table.find_elements(By.TAG_NAME, "tr")[1:]
        top_row = None
        top_liquidity = -1
        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 7:
                continue
            try:
                liquidity = int(cells[liquidity_idx].text.replace(',', '').replace('--', '0').strip())
            except:
                liquidity = 0
            if liquidity > top_liquidity:
                top_liquidity = liquidity
                top_row = cells

        if not top_row:
            driver.quit()
            return []

        # Extract info from the top DEX row
        exchange = top_row[1].text.strip()
        pair = top_row[2].text
        price = top_row[3].text
        volume_24h = top_row[header_texts.index("volume (24h)")].text if "volume (24h)" in header_texts else ""
        liquidity = top_row[liquidity_idx].text

        # Get the pair link URL (the blue link)
        link = top_row[2].find_element(By.TAG_NAME, "a")
        pair_url = link.get_attribute("href")

        # Open the pair link in a new tab using JavaScript
        driver.execute_script(f"window.open('{pair_url}', '_blank');")
        time.sleep(1)
        driver.switch_to.window(driver.window_handles[-1])

        # Find all info boxes in the top row (they are usually direct children of a parent div)
        info_boxes = driver.find_elements(By.XPATH, "//div[contains(@class,'sc') and .//div[text()='Liquidity']]")
        final_liquidity = None
        for box in info_boxes:
            try:
                # Find the label
                label = box.find_element(By.XPATH, ".//div[text()='Liquidity']")
                # The value is usually in a sibling or following div
                value = label.find_element(By.XPATH, "../div[contains(text(), '$')]")
                final_liquidity = value.text.strip()
                # Highlight for debugging
                driver.execute_script("arguments[0].style.border='3px solid blue'; arguments[0].style.background='#e0f0ff';", value)
                break
            except Exception as e:
                continue
        # Fallback: use old method if not found
        if not final_liquidity:
            final_liquidity = extract_value_by_label(driver, "liquidity")
        # Always wait 15 seconds for visual confirmation
        time.sleep(15)
    except Exception as e:
        print(f"Error extracting liquidity from pair page: {e}")
        with open("pair_page_source_failed.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        final_liquidity = None
        time.sleep(15)
    driver.close()  # Close the pair tab
    driver.switch_to.window(driver.window_handles[0])  # Switch back to main tab

    driver.quit()
    return [{
        "exchange": exchange,
        "pair": pair,
        "price": price,
        "volume_24h": volume_24h,
        "liquidity": liquidity,
        "final_liquidity": final_liquidity or 'N/A'
    }]

def parse_liquidity(liquidity_str):
    try:
        return float(liquidity_str.replace(',', '').replace('--', '0').strip())
    except:
        return 0

def get_top_cex_markets_by_liquidity(slug, limit=3):
    url = f"https://coinmarketcap.com/currencies/{slug}/markets/"
    driver = get_webdriver()
    driver.get(url)
    time.sleep(5)

    try:
        consent_button = driver.find_element(By.XPATH, "//button[contains(., 'Accept')]")
        consent_button.click()
        time.sleep(1)
    except:
        pass

    try:
        cex_tab = driver.find_element(By.XPATH, "//button[contains(., 'CEX')]")
        cex_tab.click()
        time.sleep(2)
    except:
        pass

    table = driver.find_element(By.TAG_NAME, "table")
    headers = table.find_elements(By.TAG_NAME, "th")
    header_texts = [h.text.strip().lower() for h in headers]

    try:
        liquidity_idx = header_texts.index("liquidity score")
    except ValueError:
        liquidity_idx = None

    cex_markets = []
    cex_names = ["Binance", "Bybit", "Bitget", "MEXC", "Gate.io", "KuCoin", "Crypto.com Exchange", "OKX"]
    row_elements = []  # Store row elements for highlighting

    rows = table.find_elements(By.TAG_NAME, "tr")[1:]
    for row in rows:
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) < 7 or liquidity_idx is None:
            continue
        exchange = cells[1].text
        if any(cex.lower() in exchange.lower() for cex in cex_names):
            pair = cells[2].text
            price = cells[3].text
            volume_24h = cells[header_texts.index("volume (24h)")].text if "volume (24h)" in header_texts else ""
            liquidity = cells[liquidity_idx].text
            cex_markets.append({
                "exchange": exchange,
                "pair": pair,
                "price": price,
                "volume_24h": volume_24h,
                "liquidity": liquidity,
                "liquidity_num": parse_liquidity(liquidity)
            })
            row_elements.append(row)

    # Sort and get top N
    cex_markets_with_rows = list(zip(cex_markets, row_elements))
    cex_markets_with_rows.sort(key=lambda x: x[0]["liquidity_num"], reverse=True)
    top_cex_markets_with_rows = cex_markets_with_rows[:limit]

    # Highlight top N rows in blue
    for _, row in top_cex_markets_with_rows:
        highlight_element(driver, row, color='blue', background='#e0f0ff')
        # Optionally, scroll into view
        driver.execute_script("arguments[0].scrollIntoView(true);", row)
        time.sleep(0.5)

    # Remove helper key before returning
    top_cex_markets = [m for m, _ in top_cex_markets_with_rows]
    for m in top_cex_markets:
        m.pop("liquidity_num", None)

    # Wait a bit so user can see highlights before closing
    time.sleep(3)
    driver.quit()

    return top_cex_markets

def get_market_cap_and_volume(slug):
    url = f"https://coinmarketcap.com/currencies/{slug}/"
    driver = get_webdriver()
    driver.get(url)
    time.sleep(5)

    try:
        consent_button = driver.find_element(By.XPATH, "//button[contains(., 'Accept')]")
        consent_button.click()
        time.sleep(1)
    except:
        pass

    market_cap = None
    volume_24h = None

    try:
        dls = driver.find_elements(By.TAG_NAME, "dl")
        for dl in dls:
            dts = dl.find_elements(By.TAG_NAME, "dt")
            dds = dl.find_elements(By.TAG_NAME, "dd")
            for dt, dd in zip(dts, dds):
                label = dt.text.strip()
                value = dd.text.strip()
                # Only match label exactly "Market cap"
                if label.lower() == "market cap":
                    market_cap = value
                elif "volume" in label.lower():
                    volume_24h = value
    except Exception as e:
        print("Error extracting market cap or volume:", e)

    driver.quit()
    return {"market_cap": market_cap, "volume_24h": volume_24h}

@app.route('/crypto/contracts/<slug>', methods=['GET'])
def get_contract(slug):
    headers = {
        'Accepts': 'application/json',
        'X-CMC_PRO_API_KEY': CMC_API_KEY
    }

    crypto_id = get_id_from_slug(slug)
    token_info = {}
    if crypto_id:
        params = {'id': crypto_id}
        response = requests.get(CMC_INFO_URL, headers=headers, params=params)
        info = response.json()
        token_info = info['data'][str(crypto_id)]
        token_name = token_info.get('name', slug)
    else:
        # Fallback: use slug as token name if API fails
        token_name = slug

    top_cex_market = get_top_cex_markets_by_liquidity(slug)
    top_dex_market = get_top_dex_market_selenium(slug)
    market_stats = get_market_cap_and_volume(slug)
    
    result = {
        'name': token_name,
        'symbol': token_info.get('symbol', slug.upper()),
        'contract_address': token_info.get('platform', {}).get('token_address', 'N/A') if token_info else 'N/A',
        'platform': token_info.get('platform', {}).get('name', 'N/A') if token_info else 'N/A',
        'top_cex_market': top_cex_market,
        'top_dex_market': top_dex_market,
        'market_cap': market_stats['market_cap'],
        'volume_24h': market_stats['volume_24h']
    }

    result['investment_commitment'] = get_investment_commitment(result)

    # Extract values
    Min, Max, commitment, Investment = extract_investment_values(result['investment_commitment'])

    # Add to JSON
    result["Min"] = Min
    result["Max"] = Max
    result["commitment"] = commitment
    result["Investment"] = Investment

    return jsonify(result)

def extract_slug_from_url(url):
    match = re.search(r'coinmarketcap\.com/currencies/([^/]+)', url)
    if match:
        return match.group(1)
    return None

@app.route('/notify_investment_proposal', methods=['POST'])
def notify_investment_proposal():
    data = request.get_json()
    url = data.get('url')
    if url and CMC_URL_REGEX.match(url):
        slug = extract_slug_from_url(url)
        if slug:
            # Initial response message
            initial_message = "I'm on it! I'm working on generating an investment proposal based on the details provided in the link."
            payload = {
                'chat_id': TELEGRAM_CHAT_ID,
                'text': initial_message
            }
            resp = requests.post(TELEGRAM_API_URL, json=payload)
            print(resp.text)
            
            try:
                # First get basic token info
                headers = {
                    'Accepts': 'application/json',
                    'X-CMC_PRO_API_KEY': CMC_API_KEY
                }
                crypto_id = get_id_from_slug(slug)
                if not crypto_id:
                    error_msg = f"No token found for slug '{slug}'"
                    send_telegram_message(TELEGRAM_CHAT_ID, error_msg)
                    return jsonify({'error': error_msg}), 404

                params = {'id': crypto_id}
                response = requests.get(CMC_INFO_URL, headers=headers, params=params)
                info = response.json()
                token_info = info['data'][str(crypto_id)]
                
                # Now run Selenium operations
                print("Starting Selenium operations...")
                top_cex_market = get_top_cex_markets_by_liquidity(slug)
                print("CEX markets fetched")
                top_dex_market = get_top_dex_market_selenium(slug)
                print("DEX markets fetched")
                market_stats = get_market_cap_and_volume(slug)
                print("Market stats fetched")
                
                # Compile results
                result = {
                    'name': token_info['name'],
                    'symbol': token_info['symbol'],
                    'contract_address': token_info.get('platform', {}).get('token_address', 'N/A'),
                    'platform': token_info.get('platform', {}).get('name', 'N/A'),
                    'top_cex_market': top_cex_market,
                    'top_dex_market': top_dex_market,
                    'market_cap': market_stats['market_cap'],
                    'volume_24h': market_stats['volume_24h']
                }

                # Get investment commitment
                result['investment_commitment'] = get_investment_commitment(result)
                print("[DEBUG] Investment commitment calculated")

                if 'the token i fetch doesnt exist in this following' in result['investment_commitment']:
                    send_telegram_message(TELEGRAM_CHAT_ID, result['investment_commitment'])
                    return jsonify({'ok': True})

                # Try to get values from JSON first
                Min = result.get("Min")
                Max = result.get("Max")
                commitment = result.get("commitment")
                Investment = result.get("Investment")

                # Fallback to parsing if any are missing
                if not all([Min, Max, commitment, Investment]):
                    Min, Max, commitment, Investment = extract_investment_values(result['investment_commitment'])

                if not all([Min, Max, commitment, Investment]):
                    error_msg = "Could not extract investment values from commitment"
                    send_telegram_message(TELEGRAM_CHAT_ID, error_msg)
                    return jsonify({'error': error_msg}), 500

                # Generate and send the formatted proposal message
                proposal_token_name = token_name  # token_name is set to API name or slug above

                formatted_message = proposal_message_from_vars(
                    token_name=proposal_token_name,
                    Investment=Investment,
                    commitment=commitment,
                    Min=Min,
                    Max=Max,
                    slug=slug
                )
                
                print("[DEBUG] Sending formatted proposal...")
                send_telegram_message(TELEGRAM_CHAT_ID, formatted_message)
                print("[DEBUG] Proposal sent successfully")
                
                return jsonify({'status': 'success', 'message': 'Notification and proposal sent to Telegram.'})
                    
            except Exception as e:
                error_message = f"Failed to generate proposal: {str(e)}"
                print(f"Error: {error_message}")
                send_telegram_message(TELEGRAM_CHAT_ID, error_message)
                return jsonify({'status': 'error', 'message': error_message}), 500
                
    return jsonify({'status': 'ignored', 'message': 'URL does not match CoinMarketCap pattern.'}), 400

# Add this after the imports
def create_telegram_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=3,  # number of retries
        backoff_factor=1,  # wait 1, 2, 4 seconds between retries
        status_forcelist=[500, 502, 503, 504]  # HTTP status codes to retry on
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# Create a persistent session for Telegram
telegram_session = create_telegram_session()

# Helper to send Telegram message
def send_telegram_message(chat_id, text, max_retries=3):
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': False
    }
    
    for attempt in range(max_retries):
        try:
            # Use the persistent session with retry logic
            resp = telegram_session.post(TELEGRAM_API_URL, data=payload, timeout=10)
            resp.raise_for_status()  # Raise an exception for bad status codes
            print(f"[DEBUG] Telegram API response: {resp.text}")
            return True
        except requests.exceptions.RequestException as e:
            print(f"[DEBUG] Attempt {attempt + 1}/{max_retries} failed to send Telegram message: {str(e)}")
            if attempt < max_retries - 1:
                # Wait before retrying (exponential backoff)
                wait_time = (2 ** attempt) * 1  # 1, 2, 4 seconds
                print(f"[DEBUG] Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
            else:
                print(f"[ERROR] Failed to send Telegram message after {max_retries} attempts: {str(e)}")
                # Try one last time with a fresh session
                try:
                    fresh_session = create_telegram_session()
                    resp = fresh_session.post(TELEGRAM_API_URL, data=payload, timeout=10)
                    resp.raise_for_status()
                    print("[DEBUG] Successfully sent message with fresh session")
                    return True
                except Exception as final_e:
                    print(f"[ERROR] Final attempt failed: {str(final_e)}")
                    return False
    return False

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    global last_processed_message_id_per_chat, last_message_time_per_chat, spam_attempts_per_chat, chat_locks
    data = request.get_json()
    message = data.get('message', {})
    chat_id = message.get('chat', {}).get('id')
    text = message.get('text', '')
    message_id = message.get('message_id')
    current_time = datetime.now()

    # Ensure per-chat lock exists
    if chat_id not in chat_locks:
        chat_locks[chat_id] = threading.Lock()

    with chat_locks[chat_id]:
        # Check if user is currently blocked
        if chat_id in spam_attempts_per_chat:
            last_spam_time = spam_attempts_per_chat[chat_id].get('block_until')
            if last_spam_time and current_time < last_spam_time:
                send_telegram_message(chat_id, f"please don't spam me 🥺 please wait for {BLOCK_DURATION}secs")
                return jsonify({'ok': True})
            elif last_spam_time and current_time >= last_spam_time:
                # Block just expired, send friendly message
                send_telegram_message(chat_id, "you can chat me again with another link now 😊 but please dont spam me again i get dizzy 😵‍💫")
                # Remove block
                del spam_attempts_per_chat[chat_id]

        last_time = last_message_time_per_chat.get(chat_id)
        # If message is within cooldown, set block immediately and return
        if last_time and (current_time - last_time) < timedelta(seconds=COOLDOWN_SECONDS):
            # Set block for BLOCK_DURATION seconds IMMEDIATELY and return
            block_until = current_time + timedelta(seconds=BLOCK_DURATION)
            spam_attempts_per_chat[chat_id] = {'block_until': block_until}
            last_message_time_per_chat[chat_id] = current_time  # update to prevent further races
            send_telegram_message(chat_id, f"please don't spam me 🥺 please wait for {BLOCK_DURATION}secs")
            return jsonify({'ok': True})
        # If not in cooldown, set last_message_time_per_chat right away to prevent race
        last_message_time_per_chat[chat_id] = current_time

        if chat_id:
            # Extract CMC URL from message
            cmc_url_match = CMC_URL_REGEX.search(text)
            if not cmc_url_match:
                # Not a CoinMarketCap link
                send_telegram_message(chat_id, "Oh no you send a wrong link try it again it should be related to coinmarketcap link")
                return jsonify({'ok': True})
            # Only process if this message_id is new for this chat
            if last_processed_message_id_per_chat.get(chat_id) == message_id:
                print("[DEBUG] Duplicate message, skipping.")
                return jsonify({'ok': True})
            last_processed_message_id_per_chat[chat_id] = message_id

            print("[DEBUG] CMC URL detected, sending reply...")
            
            # Send initial response
            send_telegram_message(chat_id, "I'm on it! I'm working on generating an investment proposal based on the details provided in the link.")
            
            try:
                # Extract slug from URL
                slug = extract_slug_from_url(text)
                if not slug:
                    send_telegram_message(chat_id, "your link structure was wrong try again")
                    return jsonify({'error': 'Malformed CoinMarketCap link'}), 400
                    
                print(f"[DEBUG] Processing token: {slug}")
                
                # Get basic token info
                headers = {
                    'Accepts': 'application/json',
                    'X-CMC_PRO_API_KEY': CMC_API_KEY
                }
                crypto_id = get_id_from_slug(slug)
                token_info = {}
                if crypto_id:
                    params = {'id': crypto_id}
                    response = requests.get(CMC_INFO_URL, headers=headers, params=params)
                    info = response.json()
                    token_info = info['data'][str(crypto_id)]
                    token_name = token_info.get('name', slug)
                else:
                    # Fallback: use slug as token name if API fails
                    token_name = slug

                # Proceed with Selenium and market scraping regardless
                print("[DEBUG] Starting Selenium operations...")
                send_telegram_message(chat_id, "Fetching market data...")
                
                top_cex_market = get_top_cex_markets_by_liquidity(slug)
                print("[DEBUG] CEX markets fetched")
                
                top_dex_market = get_top_dex_market_selenium(slug)
                print("[DEBUG] DEX markets fetched")
                
                market_stats = get_market_cap_and_volume(slug)
                print("[DEBUG] Market stats fetched")
                
                # Compile results
                result = {
                    'name': token_name,
                    'symbol': token_info.get('symbol', slug.upper()),
                    'contract_address': token_info.get('platform', {}).get('token_address', 'N/A') if token_info else 'N/A',
                    'platform': token_info.get('platform', {}).get('name', 'N/A') if token_info else 'N/A',
                    'top_cex_market': top_cex_market,
                    'top_dex_market': top_dex_market,
                    'market_cap': market_stats['market_cap'],
                    'volume_24h': market_stats['volume_24h']
                }

                # Get investment commitment
                result['investment_commitment'] = get_investment_commitment(result)
                print("[DEBUG] Investment commitment calculated")

                if 'the token i fetch doesnt exist in this following' in result['investment_commitment']:
                    send_telegram_message(TELEGRAM_CHAT_ID, result['investment_commitment'])
                    return jsonify({'ok': True})

                # Try to get values from JSON first
                Min = result.get("Min")
                Max = result.get("Max")
                commitment = result.get("commitment")
                Investment = result.get("Investment")

                # Fallback to parsing if any are missing
                if not all([Min, Max, commitment, Investment]):
                    Min, Max, commitment, Investment = extract_investment_values(result['investment_commitment'])

                if not all([Min, Max, commitment, Investment]):
                    error_msg = "Could not extract investment values from commitment"
                    send_telegram_message(TELEGRAM_CHAT_ID, error_msg)
                    return jsonify({'error': error_msg}), 500

                # Generate and send the formatted proposal message
                proposal_token_name = token_name  # token_name is set to API name or slug above

                formatted_message = proposal_message_from_vars(
                    token_name=proposal_token_name,
                    Investment=Investment,
                    commitment=commitment,
                    Min=Min,
                    Max=Max,
                    slug=slug
                )
                
                print("[DEBUG] Sending formatted proposal...")
                send_telegram_message(TELEGRAM_CHAT_ID, formatted_message)
                print("[DEBUG] Proposal sent successfully")
                
                # After sending the proposal, return immediately
                return jsonify({'ok': True})
                    
            except Exception as e:
                error_message = f"Failed to generate proposal: {str(e)}"
                print(f"[DEBUG] Error: {error_message}")
                send_telegram_message(TELEGRAM_CHAT_ID, error_message)
                return jsonify({'error': error_message}), 500
            
        print("[DEBUG] No valid CMC URL or duplicate message.")
        return jsonify({'ok': True})

@app.route('/webhook', methods=['GET'])
def webhook_get():
    return 'Webhook endpoint is live!'

def extract_value_by_label(driver, label_text):
    try:
        # Wait for the <dl> element to appear
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "dl"))
        )
        dls = driver.find_elements(By.TAG_NAME, "dl")
        for dl in dls:
            dts = dl.find_elements(By.TAG_NAME, "dt")
            dds = dl.find_elements(By.TAG_NAME, "dd")
            for dt, dd in zip(dts, dds):
                label = dt.text.strip().lower()
                value = dd.text.strip()
                if label_text.lower() in label:
                    return value
        # Fallback: try to find any element with text "Liquidity" and get the next sibling
        all_elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'Liquidity')]")
        for el in all_elements:
            try:
                # Try to get the next sibling
                value = el.find_element(By.XPATH, "following-sibling::*[1]").text.strip()
                if value:
                    return value
            except:
                continue
    except Exception as e:
        print(f"Error extracting {label_text}: {e}")
    return None

def parse_dollar(val):
    if not val:
        return 0
    # Use regex to extract the first number (with optional decimal and T/B/M/K)
    match = re.search(r"([\d,.]+)\s*([TBMK]?)", val.replace('$','').replace('\n',' '))
    if not match:
        return 0
    num, suffix = match.groups()
    num = num.replace(',', '')
    try:
        value = float(num)
    except:
        return 0
    if suffix == 'T':
        value *= 1_000_000_000_000
    elif suffix == 'B':
        value *= 1_000_000_000
    elif suffix == 'M':
        value *= 1_000_000
    elif suffix == 'K':
        value *= 1_000
    return int(value)

def get_investment_commitment(result):
    trace = []
    def parse_dollar(val):
        if not val:
            return 0
        match = re.search(r"([\d,.]+)\s*([TBMK]?)", val.replace('$','').replace('\n',' '))
        if not match:
            return 0
        num, suffix = match.groups()
        num = num.replace(',', '')
        try:
            value = float(num)
        except:
            return 0
        if suffix == 'T':
            value *= 1_000_000_000_000
        elif suffix == 'B':
            value *= 1_000_000_000
        elif suffix == 'M':
            value *= 1_000_000
        elif suffix == 'K':
            value *= 1_000
        return int(value)

    # 1. Market cap > $1m?
    market_cap_val = parse_dollar(result.get('market_cap', ''))
    trace.append(f"Market cap: {market_cap_val} (raw: {result.get('market_cap', '')})")
    if market_cap_val <= 1_000_000:
        trace.append("Market cap is less than $1M. Skipping.")
        return " -> ".join(trace)

    # 2. 24h Volume > 150k?
    volume_24h_val = parse_dollar(result.get('volume_24h', ''))
    trace.append(f"24h Volume: {volume_24h_val} (raw: {result.get('volume_24h', '')})")
    if volume_24h_val <= 150_000:
        trace.append("24h volume is less than $150K. Skipping.")
        return " -> ".join(trace)

    # 3. Listed on T1/T2 CEX?
    t1 = ["Binance", "Coinbase", "OKX", "Bybit", "Gate", "KuCoin", "Kraken"]
    t2 = ["BitMart", "MEXC", "Bitget", "LBank", "Coinstore", "CoinEx", "HTX", "Weex"]
    cex_markets = result.get('top_cex_market', [])
    cex_markets_t1_t2 = [c for c in cex_markets if any(t in c['exchange'] for t in t1 + t2)]
    trace.append(f"does cex exist? {'yes' if cex_markets_t1_t2 else 'no'}")
    if not cex_markets_t1_t2:
        return ("the token i fetch doesnt exist in this following\n"
                "Tier 1: Binance, Coinbase, OKX, Bybit, Gate, KuCoin, Kraken\n"
                "Tier 2: BitMart, MEXC, Bitget, LBank, Coinstore, CoinEx,HTX,Weex\n\n"
                "try again with another token")

    # If DEX exists, use DEX logic only
    dex_markets = result.get('top_dex_market', [])
    trace.append(f"does dex exist? {'yes' if dex_markets and len(dex_markets) > 0 else 'no'}")
    if dex_markets and len(dex_markets) > 0:
        dex = dex_markets[0]
        dex_liquidity = parse_dollar(dex.get('final_liquidity') or dex.get('liquidity'))
        trace.append(f"DEX Liquidity: {dex_liquidity} (raw: {dex.get('final_liquidity') or dex.get('liquidity')})")
        if dex_liquidity < 25_000:
            trace.append(f"DEX liquidity is less than $25K. Skipping. (Liquidity: {dex_liquidity})")
            return " -> ".join(trace)
        elif 25_000 < dex_liquidity <= 50_000:
            trace.append("DEX liquidity 25K-50K. Daily transaction 500 - 1K minimum commitment 250K investment 350K")
            return " -> ".join(trace)
        elif 50_000 < dex_liquidity <= 100_000:
            trace.append("DEX liquidity 50K-100K. Daily transaction 1K - 2.5K minimum commitment 350K investment 500K")
            return " -> ".join(trace)
        elif 100_000 < dex_liquidity <= 250_000:
            trace.append("DEX liquidity 100K-250K. Daily transaction 2.5K - 5K minimum commitment 500K investment 600K")
            return " -> ".join(trace)
        elif 250_000 < dex_liquidity <= 1_000_000:
            trace.append("DEX liquidity 250K-1M. Daily transaction 5K - 10K minimum commitment 600K investment 800K")
            return " -> ".join(trace)
        elif 1_000_000 < dex_liquidity <= 3_000_000:
            trace.append("DEX liquidity 1M-3M. Daily transaction 10K - 25K minimum commitment 800K investment 1M")
            return " -> ".join(trace)
        elif dex_liquidity > 3_000_000:
            trace.append("DEX liquidity >3M. Daily transaction 25K - 40K minimum commitment 1M investment 3M")
            return " -> ".join(trace)
        trace.append("No suitable investment found.")
        return " -> ".join(trace)
    # If no DEX, use CEX logic only
    cex = max(cex_markets_t1_t2, key=lambda x: int(x.get('liquidity', '0').replace(',', '')))
    cex_vol = parse_dollar(result.get('volume_24h', ''))
    cex_liq = int(cex.get('liquidity', '0').replace(',', ''))
    trace.append(f"CEX Volume: {cex_vol}, CEX Liquidity: {cex_liq}")
    if cex_vol < 150_000 and 1 <= cex_liq <= 150:
        trace.append("CEX volume < 150K and liquidity score 1-150. Skipping.")
        return " -> ".join(trace)
    elif cex_vol > 150_000 and 150 < cex_liq <= 250:
        trace.append("CEX volume >150K and liquidity score 150-250. Skipping.")
        return " -> ".join(trace)
    elif 150_000 < cex_vol <= 250_000 and 250 < cex_liq <= 350:
        trace.append("CEX volume 150K-250K and liquidity score 250-350. Daily transaction 500 - 1K minimum commitment 250K investment 350K")
        return " -> ".join(trace)
    elif 250_000 < cex_vol <= 500_000 and 350 < cex_liq <= 450:
        trace.append("CEX volume 250K-500K and liquidity score 350-450. Daily transaction 1K - 2.5K minimum commitment 350K investment 500K")
        return " -> ".join(trace)
    elif 500_000 < cex_vol and 450 < cex_liq <= 550:
        trace.append("CEX volume >500K and liquidity score 450-550. Daily transaction 2.5K - 5K minimum commitment 500K investment 600K")
        return " -> ".join(trace)
    elif 1_000_000 < cex_vol and 550 < cex_liq <= 600:
        trace.append("CEX volume >1M and liquidity score 550-600. Daily transaction 5K - 10K minimum commitment 600K investment 800K")
        return " -> ".join(trace)
    elif 3_000_000 < cex_vol and cex_liq > 600:
        trace.append("CEX volume >3M and liquidity score >600. Daily transaction 10K - 25K minimum commitment 800K investment 1M")
        return " -> ".join(trace)
    trace.append("No suitable investment found.")
    return " -> ".join(trace)

def format_proposal(token_name, dex_trace):
    # Example: "DEX liquidity >3M. Daily transaction 25K - 40K minimum commitment 1M investment 3M"
    import re
    # Extract daily transaction and commitment/investment
    daily_txn = re.search(r"Daily transaction ([\d\.K]+) - ([\d\.K]+)", dex_trace)
    min_commit = re.search(r"minimum commitment ([\d\.MK]+)", dex_trace)
    invest = re.search(r"investment ([\d\.MK]+)", dex_trace)
    if not (daily_txn and min_commit and invest):
        return dex_trace  # fallback to raw trace if parsing fails

    min_daily = daily_txn.group(1)
    max_daily = daily_txn.group(2)
    min_commitment = min_commit.group(1)
    investment = invest.group(1)

    return f'''📄 PROPOSAL FORMAT:
Investment Proposal - {token_name}
Investment: ${investment}
Minimum commitment: ${min_commitment}
Discount: 22%
Trial period: 2 weeks.
Investment will be made in daily transactions of ${min_daily} total per day, then
increase to ${min_daily} - ${max_daily} per day after trial period. Price will be determined using the current,
daily market price at the time of transaction/ event.
✅ Meet our team, review testimonials & explore our value proposition VIEW OFFER HERE
(https://abrasive-alphabet-c84.notion.site/VICTUS-GLOBAL-{token_name}-customstring?pvs=4)
• Strategic business partnership support
• $1B+ Network Ecosystem
• Marketing & Elite KOL Exposure
• Connect with our CEXs partners for fast listings & discounts
• Test our professional MM service (with a limited-time 7-day free trial)
• Smart contract & Security audits
• Full-Cycle Support
Market-Making Service – 7-Day Trial
To ensure strong market positioning from the start, we provide a complimentary 7-day trial of our
professional market-making service. This includes enhanced liquidity management, optimized
order book depth, and improved trading efficiency to maximize your asset's market
performance.
Join 80+ other portfolio companies in the Victus Global network, including Pepecoin, Netmind,
Brett, Unizen, Dynex, and many more.
🔹Learn more with our Deck (https://docsend.com/view/rz6cwzaem4qj2ihi)
🔹Visit our Website (https://www.victusglobal.com/)
🔹Follow us on X (https://x.com/VictusGlobal_)
'''

def extract_investment_values(investment_commitment):
    import re
    daily_txn = re.search(r"Daily transaction ([\d\.K]+) - ([\d\.K]+)", investment_commitment)
    min_commit = re.search(r"minimum commitment ([\d\.MK]+)", investment_commitment)
    invest = re.search(r"investment ([\d\.MK]+)", investment_commitment)
    if daily_txn and min_commit and invest:
        Min = daily_txn.group(1)
        Max = daily_txn.group(2)
        commitment = min_commit.group(1)
        Investment = invest.group(1)
        return Min, Max, commitment, Investment
    return None, None, None, None

def proposal_message_from_vars(token_name, Investment, commitment, Min, Max, slug):
    token_link = f"https://coinmarketcap.com/currencies/{slug}/"
    return f'''📄 PROPOSAL FORMAT:

Investment Proposal - {token_name}

Investment: ${Investment}
Minimum commitment: ${commitment}
Discount: 22%
Trial period: 2 weeks.

Investment will be made in daily transactions of ${Min} total per day, then
increase to ${Min} - ${Max} per day after trial period. Price will be determined using the current,
daily market price at the time of transaction/ event.

✅ Meet our team, review testimonials & explore our value proposition

• Strategic business partnership support
• $1B+ Network Ecosystem
• Marketing & Elite KOL Exposure
• Connect with our CEXs partners for fast listings & discounts
• Test our professional MM service (with a limited-time 7-day free trial)
• Smart contract & Security audits
• Full-Cycle Support

Market-Making Service – 7-Day Trial
To ensure strong market positioning from the start, we provide a complimentary 7-day trial of our
professional market-making service. This includes enhanced liquidity management, optimized
order book depth, and improved trading efficiency to maximize your asset's market
performance.

Join 80+ other portfolio companies in the Victus Global network, including Pepecoin, Netmind,
Brett, Unizen, Dynex, and many more.

🔹Learn more with our Deck
https://docsend.com/view/rz6cwzaem4qj2ihi

🔹Visit our Website
{token_link}

🔹Follow us on X
https://x.com/VictusGlobal_

{token_link}
'''

def is_special_slug(slug):
    return slug in [
        "bubblemaps", "zerolend", "green-metaverse-token", "aleo",
        "ice-decentralized-future", "taiko", "frax", "manta-network", "fluence-network"
    ]

if __name__ == '__main__':
    # Use environment variable for port if available
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)


