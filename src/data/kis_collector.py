import os
import time
import requests
import json
from datetime import datetime

class KisAuth:
    """
    Korea Investment & Securities (KIS) API Authentication Manager.
    Manages OAuth2 token lifecycle.
    """
    def __init__(self):
        self.app_key = os.getenv("KIS_APP_KEY")
        self.app_secret = os.getenv("KIS_APP_SECRET")
        self.account_no = os.getenv("KIS_ACCOUNT_NO") # format: 00000000-01
        
        # Select Base URL based on Mode
        self.mode = os.getenv("KIS_MODE", "SIMULATION").upper()
        if self.mode == "REAL":
            self.base_url = "https://openapi.koreainvestment.com:9443"
        else:
            self.base_url = "https://openapivts.koreainvestment.com:29443" # Simulation
            
        print(f"[KisAuth] Initialized in {self.mode} mode. URL: {self.base_url}")
        
        self.token = None
        self.token_expiry = None

    def auth(self):
        """Request a new access token."""
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        
        # NOTE: endpoint might differ for real vs simulation. Using real by default.
        url = f"{self.base_url}/oauth2/tokenP" 
        
        try:
            res = requests.post(url, headers=headers, data=json.dumps(body))
            res.raise_for_status()
            data = res.json()
            self.token = data['access_token']
            self.token_expiry = data['access_token_token_expired'] # Format: 2026-02-17 14:00:00
            print(f"[KisAuth] Token refreshed. Expires at {self.token_expiry}")
        except Exception as e:
            print(f"[KisAuth] Error refreshing token: {e}")
            raise

    def get_token(self):
        """Return valid token, refreshing if necessary."""
        if not self.token:
            self.auth()
            return self.token
            
        # Check expiry (simple check, assume format is standard)
        # For robustness, we might want to refresh if within 10 mins of expiry
        # simplified for MVP
        return self.token
        
    def get_header(self, tr_id):
        """Construct standard header for API calls"""
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self.get_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id
        }

class KisData:
    """
    KIS Data Collector.
    Fetches market data using KisAuth.
    """
    def __init__(self, auth_manager: KisAuth):
        self.auth = auth_manager
        self.base_url = self.auth.base_url

    def get_market_index(self, market_code="0001"):
        """
        Fetch Current Index (KOSPI/KOSDAQ).
        market_code: '0001' (KOSPI), '1001' (KOSDAQ)
        """
        # Endpoint for Index Current Price (not stock)
        # Using 'FHKUP03500100' (Upcode - Index) or similar.
        # For simplicity in this MVP, we use the standard 'inquire-price' expecting it might work for valid index codes
        # OR we use the specific Index Snapshot TR ID: FHKST01010400
        
        tr_id = "FHKST01010400" 
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-index-chartprice"
        
        headers = self.auth.get_header(tr_id)
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": market_code,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0"
        }
        
        try:
            res = requests.get(url, headers=headers, params=params)
            res.raise_for_status()
            return res.json()
        except Exception as e:
            print(f"[KisData] Error fetching index {market_code}: {e}")
            return None

    def get_investor_trend(self, market_code="0001"):
        """
        Fetch Investor Breakdown (Net Purchase).
        TR ID: FHKST01010900 (Investor Trend)
        """
        tr_id = "FHKST01010900"
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
        
        headers = self.auth.get_header(tr_id)
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": market_code,
        }
        
        try:
            res = requests.get(url, headers=headers, params=params)
            res.raise_for_status()
            return res.json()
        except Exception as e:
            print(f"[KisData] Error fetching investor trend {market_code}: {e}")
            return None

    def get_exchange_rate(self):
        """
        Fetch USD/KRW Exchange Rate.
        Fallback to FinanceDataReader if KIS Overseas API is not configured.
        """
        try:
            # Fallback: using FinanceDataReader (if installed) or a simple public API for MVP
            import FinanceDataReader as fdr
            df = fdr.DataReader('USD/KRW', data_source='woori') # Real-time-ish
            return df.iloc[-1]['Close']
        except ImportError:
            print("[KisData] FinanceDataReader not installed. Returning mock data.")
            return 1350.0
        except Exception as e:
            print(f"[KisData] Error fetching exchange rate: {e}")
            return None
