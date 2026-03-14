import os
import json
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
import yfinance as yf
import pandas as pd
import uvicorn
import threading
import time
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore, messaging, auth

# --- VERİ MODELLERİ ---
class AnalysisRequest(BaseModel):
    symbol: str
    rsi_interval: str = "1h"
    price_interval: str = "1h"

# --- FIREBASE KURULUMU ---
try:
    env_creds = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if env_creds:
        cred_dict = json.loads(env_creds)
        cred = credentials.Certificate(cred_dict)
    else:
        cred = credentials.Certificate("serviceAccountKey.json")

    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firebase Bağlantısı Başarılı")
except Exception as e:
    print(f"❌ Firebase Bağlantı Hatası: {e}")
    db = None

ALARM_COOLDOWNS = {} 
COOLDOWN_SECONDS = 3600 

# --- ARKA PLAN GÖREVLERİ ---

def alarm_monitor_system():
    print("🔔 Alarm Takip Sistemi Başlatıldı...")
    while True:
        if not db:
            time.sleep(60)
            continue
        try:
            users_ref = db.collection('users').stream()
            for user in users_ref:
                user_data = user.to_dict()
                fcm_token = user_data.get('fcmToken')
                user_id = user.id
                if not fcm_token: continue
                
                alarms_ref = db.collection('users').document(user_id).collection('alarms').stream()
                for alarm in alarms_ref:
                    alarm_data = alarm.to_dict()
                    if alarm_data.get('isTriggered') == True: continue

                    symbol = alarm_data.get('symbol')
                    indicator = alarm_data.get('indicator')
                    condition = alarm_data.get('condition')
                    try:
                        threshold = float(alarm_data.get('value', 0))
                    except: continue

                    cooldown_key = alarm.id
                    last_notification = ALARM_COOLDOWNS.get(cooldown_key)
                    if last_notification and (time.time() - last_notification) < COOLDOWN_SECONDS:
                        continue

                    stock_data = process_stock_analysis(symbol, "1h", "1h")
                    if not stock_data: continue
                        
                    triggered = False
                    body_msg = ""

                    # --- ALARM MANTIĞI ---
                    if indicator == 'price':
                        val = stock_data.get('price', 0)
                        if (condition == 'gt' and val > threshold) or (condition == 'lt' and val < threshold):
                            triggered = True
                            body_msg = f"{symbol} fiyatı {threshold} sınırını geçti. Şu an: {val:.2f}"

                    elif indicator == 'rsi':
                        val = stock_data.get('rsi', 0)
                        if (condition == 'gt' and val > threshold) or (condition == 'lt' and val < threshold):
                            triggered = True
                            body_msg = f"{symbol} RSI değeri {threshold} sınırını geçti. Şu an: {val:.2f}"

                    elif indicator == 'macd':
                        # MACD Sinyal Hattı Kesişim Kontrolü
                        curr_m = stock_data.get('macd')
                        curr_s = stock_data.get('macd_signal')
                        prev_m = stock_data.get('prev_macd')
                        prev_s = stock_data.get('prev_macd_signal')

                        # GT (Büyükse) -> Alttan Yukarı Kesme (Golden Cross)
                        if condition == 'gt' and prev_m <= prev_s and curr_m > curr_s:
                            triggered = True
                            body_msg = f"🚀 {symbol}: MACD sinyal hattını ALTTAN yukarı kesti!"
                        
                        # LT (Küçükse) -> Üstten Aşağı Kesme (Death Cross)
                        elif condition == 'lt' and prev_m >= prev_s and curr_m < curr_s:
                            triggered = True
                            body_msg = f"⚠️ {symbol}: MACD sinyal hattını ÜSTTEN aşağı kesti!"
                    
                    if triggered:
                        print(f"🚨 TETİKLENDİ: {symbol} - {indicator}")
                        send_push_notification(
                            token=fcm_token,
                            title=f"Indihunt Alarm: {symbol}",
                            body=body_msg,
                            user_id=user_id,
                            alarm_id=alarm.id
                        )
                        db.collection('users').document(user_id).collection('alarms').document(alarm.id).update({
                            'isTriggered': True,
                            'lastTriggeredAt': firestore.SERVER_TIMESTAMP
                        })
                        ALARM_COOLDOWNS[cooldown_key] = time.time()
                        
        except Exception as e:
            print(f"⚠️ Hata: {e}")
        time.sleep(60)

def send_push_notification(token, title, body, user_id=None, alarm_id=None):
    try:
        app = firebase_admin.get_app()
        access_token = app.credential.get_access_token().access_token
        project_id = os.environ.get("PROJECT_ID")
        url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
        headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json; UTF-8'}
        payload = {
        "message": {
            "token": token,
            "notification": {
                "title": title,
                "body": body
            },
            "android": {
                "priority": "high", # Cihazı derin uykudan uyandırır
                "notification": {
                    "sound": "default", # Android için varsayılan ses
                    "default_vibrate_timings": True, # Varsayılan titreşim
                    "tag": str(alarm_id) if alarm_id else "default"
                }
            },
            "apns": {
                "payload": {
                    "aps": {
                        "sound": "default", # iOS için varsayılan ses
                        "content-available": 1 # Arka plan işlemleri için
                    }
                },
                "headers": {
                    "apns-collapse-id": str(alarm_id) if alarm_id else "default",
                    "apns-priority": "10" # iOS anlık gönderim önceliği
                }
            }
        }
    }
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            print(f"📨 Bildirim Gönderildi: {response.json()}")
        else:
            print(f"❌ FCM V1 Hatası (Kod: {response.status_code}): {response.text}")
            
            # Ölü token temizliği (Uygulamayı silen kullanıcılar için)
            if "UNREGISTERED" in response.text or "NOT_FOUND" in response.text:
                print(f"🗑️ Ölü Token Tespit Edildi! (Kullanıcı: {user_id})")
                if user_id and db:
                    db.collection('users').document(user_id).update({'fcmToken': firestore.DELETE_FIELD})
                    print("✅ Ölü token veritabanından silindi.")
                    
    except Exception as e:
        print(f"❌ Kritik Gönderim Hatası: {e}")     
# --- ANALİZ MOTORU ---

def calculate_rsi(series, period=14):
    if len(series) < period + 1: return pd.Series([50.0] * len(series))
    delta = series.diff(1)
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_macd(series, slow=26, fast=12, signal=9):
    exp1 = series.ewm(span=fast, adjust=False).mean()
    exp2 = series.ewm(span=slow, adjust=False).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

def process_stock_analysis(symbol, rsi_interval, price_interval):
    try:
        df_tech = yf.Ticker(symbol).history(period='2mo', interval=rsi_interval, auto_adjust=True)
        if df_tech.empty or len(df_tech) < 30: return None

        rsi_series = calculate_rsi(df_tech['Close'])
        macd_line, signal_line = calculate_macd(df_tech['Close'])
        
        current_price = float(df_tech['Close'].iloc[-1])
        prev_close = df_tech['Close'].iloc[-2]
        change_val = ((current_price - prev_close) / prev_close) * 100

        return {
            "symbol": symbol,
            "price": current_price,
            "rsi": float(rsi_series.iloc[-1]),
            "macd": float(macd_line.iloc[-1]),
            "macd_signal": float(signal_line.iloc[-1]),
            "prev_macd": float(macd_line.iloc[-2]),
            "prev_macd_signal": float(signal_line.iloc[-2]),
            "change": float(change_val)
        }
    except Exception as e:
        return None

# --- FASTAPI SETUP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Alarm sistemini ayrı bir thread'de başlat
    alarm_thread = threading.Thread(target=alarm_monitor_system, daemon=True)
    alarm_thread.start()
    
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root(): return {"status": "running"}

@app.post("/api/analyze")
def analyze_stock(request: AnalysisRequest):
    data = process_stock_analysis(request.symbol.upper(), request.rsi_interval, request.price_interval)
    if data: return {"status": "success", "data": data}
    raise HTTPException(status_code=404, detail="Veri bulunamadı.")

@app.delete("/api/users/{user_id}")
def delete_user_account(user_id: str):
    try:
        alarms_ref = db.collection('users').document(user_id).collection('alarms').stream()
        for a in alarms_ref: a.reference.delete()
        db.collection('users').document(user_id).delete()
        auth.delete_user(user_id)
        return {"status": "success"}
    except: raise HTTPException(status_code=500)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
