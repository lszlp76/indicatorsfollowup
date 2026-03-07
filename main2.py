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
    # Önce Environment Variable kontrolü yap (Render için)
    env_creds = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    
    if env_creds:
        print("🌍 Render ortamı algılandı, kimlik bilgileri Environment Variable'dan okunuyor...")
        cred_dict = json.loads(env_creds)
        cred = credentials.Certificate(cred_dict)
    else:
        # Yoksa yerel dosyaya bak (Kendi bilgisayarın için)
        print("💻 Yerel ortam algılandı, dosya okunuyor...")
        cred = credentials.Certificate("serviceAccountKey.json")

    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firebase Bağlantısı Başarılı")
    
except Exception as e:
    print(f"❌ Firebase Bağlantı Hatası: {e}")
    db = None

# --- GLOBAL DEĞİŞKENLER ---
MARKET_CACHE = []
LAST_UPDATE = None
DEFAULT_SYMBOLS = ['THYAO.IS', 'GARAN.IS', 'BTC-USD', 'ETH-USD']

# Spam Bildirim Önleme Sözlüğü: {(user_id, symbol, type): timestamp}
ALARM_COOLDOWNS = {} 
COOLDOWN_SECONDS = 3600  # Aynı alarm için 1 saat boyunca tekrar bildirim atma

# --- ARKA PLAN GÖREVLERİ ---

def alarm_monitor_system():
    """Tüm kullanıcıların alarmlarını kontrol eder ve bildirim atar."""
    print("🔔 Alarm Takip Sistemi Başlatıldı...")
    
    while True:
        if not db:
            time.sleep(60)
            continue
            
        try:
            # 1. Tüm kullanıcıları getir (fcmToken'ı olanları)
            users_ref = db.collection('users').stream()
            
            for user in users_ref:
                user_data = user.to_dict()
                fcm_token = user_data.get('fcmToken')
                user_id = user.id
                
                if not fcm_token:
                    continue # Token yoksa bildirim atamayız
                
                # 2. Kullanıcının alarmlarını çek
                alarms_ref = db.collection('users').document(user_id).collection('alarms').stream()
                
                for alarm in alarms_ref:
                    alarm_data = alarm.to_dict()
                    
                    # 1. YENİ EKLENEN KOD: Eğer alarm zaten gerçekleştiyse, bunu atla (boşuna işlemci yorma)
                    if alarm_data.get('isTriggered') == True:
                        continue

                    symbol = alarm_data.get('symbol')
                    indicator = alarm_data.get('indicator') # 'price', 'rsi', 'macd'
                    condition = alarm_data.get('condition') # 'gt', 'lt'
                    try:
                        threshold = float(alarm_data.get('value', 0))
                    except:
                        continue

                    # Cooldown Kontrolü (Daha önce bildirim attık mı?)
                    # cooldown_key = (user_id, symbol, indicator, condition)
                    cooldown_key = alarm.id
                    last_notification = ALARM_COOLDOWNS.get(cooldown_key)
                    if last_notification and (time.time() - last_notification) < COOLDOWN_SECONDS:
                        continue # Henüz bekleme süresi dolmadı

                    # 3. Güncel veriyi analiz et
                    stock_data = process_stock_analysis(symbol, "1h", "1h")
                    
                    if not stock_data:
                        continue
                        
                    current_val = 0.0
                    if indicator == 'price':
                        current_val = stock_data.get('price', 0)
                    elif indicator == 'rsi':
                        current_val = stock_data.get('rsi', 0)
                    elif indicator == 'macd':
                        current_val = stock_data.get('macd', 0)
                    
                    # 4. Koşulu Kontrol Et
                    triggered = False
                    if condition == 'gt' and current_val > threshold:
                        triggered = True
                    elif condition == 'lt' and current_val < threshold:
                        triggered = True
                        
                    if triggered:
                        print(f"🚨 ALARM TETİKLENDİ: {user_id} -> {symbol} {indicator} {current_val}")
    
                        # 1. Bildirimi Gönder
                        send_push_notification(
                            token=fcm_token,
                            title=f"Alarm: {symbol}",
                            body=f"{symbol} {indicator.upper()} değeri {threshold} sınırını aştı! Şu an: {current_val:.2f}",
                            user_id=user_id,
                            alarm_id=alarm.id # <-- YENİ EKLENEN SATIR: Telefon bildirimleri ezmesin diye

                        )
    
                        # 2. VERİTABANINDA ALARMI "GERÇEKLEŞTİ" OLARAK İŞARETLE
                        try:
                            db.collection('users').document(user_id).collection('alarms').document(alarm.id).update({
                                'isTriggered': True,
                                'lastTriggeredAt': firestore.SERVER_TIMESTAMP
                            })
                        except Exception as e:
                            print(f"Alarm güncelleme hatası: {e}")
        
                        # Cooldown'a ekle
                        ALARM_COOLDOWNS[cooldown_key] = time.time()
                        
        except Exception as e:
            print(f"⚠️ Alarm Döngüsü Hatası: {e}")
            
        # Her 60 saniyede bir tüm alarmları kontrol et
        time.sleep(60)
def send_push_notification(token, title, body, user_id=None, alarm_id=None):
    try:
        # 1. Firebase Admin SDK'nın hafızasından CANLI ve GÜVENLİ bir token zorla üretiyoruz
        app = firebase_admin.get_app()
        access_token = app.credential.get_access_token().access_token
        
        # 2. Firebase Proje ID'ni doğrudan yazıyoruz (firebase_options.dart'tan aldım)
        project_id = os.environ.get("PROJECT_ID")
        
        # 3. Yeni V1 API Linki
        url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
        
        # 4. Kütüphanenin unuttuğu o yetki başlığını KENDİ ELLERİMİZLE ekliyoruz
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json; UTF-8',
        }
        
        # 5. Mesaj İçeriği (APNs ve Android ayarlarıyla birebir aynı)
        payload = {
            "message": {
                "token": token,
                "notification": {
                    "title": title,
                    "body": body
                },
                "android": {
                    "notification": {
                        "tag": str(alarm_id) if alarm_id else "default"
                    }
                },
                "apns": {
                    "headers": {
                        "apns-collapse-id": str(alarm_id) if alarm_id else "default"
                    }
                }
            }
        }
        
        # 6. İsteği Doğrudan Biz Gönderiyoruz (messaging.send kullanmadan)
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

# --- ANALİZ MOTORU ---

def calculate_rsi(series, period=14):
    if len(series) < period + 1: return 50.0
    delta = series.diff(1)
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    if avg_loss.iloc[-1] == 0: return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_macd(series, slow=26, fast=12, signal=9):
    exp1 = series.ewm(span=fast, adjust=False).mean()
    exp2 = series.ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    return macd

def determine_period(interval):
    if interval in ['1m', '2m', '5m']: return '1d'
    if interval in ['15m', '30m', '90m']: return '5d'
    if interval in ['1h', '1d']: return '2mo'
    if interval in ['1wk', '1mo']: return '2y'
    return '2mo'

def get_historical_data(symbol, interval):
    try:
        ticker = yf.Ticker(symbol)
        period = determine_period(interval)
        df = ticker.history(period=period, interval=interval, auto_adjust=True)
        return df
    except:
        return pd.DataFrame()

def process_stock_analysis(symbol, rsi_interval, price_interval):
    try:
        # 1. Teknik Veri (RSI, MACD)
        df_tech = get_historical_data(symbol, rsi_interval)
        
        if df_tech.empty or len(df_tech) < 20: 
            return None

        rsi_val = calculate_rsi(df_tech['Close']).iloc[-1]
        macd_val = calculate_macd(df_tech['Close']).iloc[-1]
        current_price = float(df_tech['Close'].iloc[-1])

        # 2. Fiyat Değişimi
        change_val = 0.0
        
        if rsi_interval == price_interval:
            prev_close = df_tech['Close'].iloc[-2] if len(df_tech) > 1 else current_price
            change_val = ((current_price - prev_close) / prev_close) * 100
        else:
            df_price = get_historical_data(symbol, price_interval)
            if not df_price.empty and len(df_price) > 1:
                last_closed_candle = df_price['Close'].iloc[-2]
                change_val = ((current_price - last_closed_candle) / last_closed_candle) * 100
            else:
                change_val = 0.0    
        
        return {
            "id": hash(symbol + rsi_interval + price_interval),
            "symbol": symbol.replace('.IS', '').replace('-USD', ''),
            "full_symbol": symbol,
            "price": current_price,
            "rsi": float(rsi_val) if not pd.isna(rsi_val) else 50.0,
            "macd": float(macd_val) if not pd.isna(macd_val) else 0.0,
            "change": float(change_val),
            "interval": rsi_interval,
            "price_interval": price_interval
        }

    except Exception as e:
        print(f"Process Error ({symbol}): {e}")
        return None

# --- ENDPOINTLER ---

@app.get("/")
def read_root():
    return {"status": "running"}

@app.post("/api/analyze")
def analyze_stock(request: AnalysisRequest):
    symbol = request.symbol.upper().strip()
    valid_intervals = ['1m', '2m', '5m', '15m', '30m', '60m', '90m', '1h', '4h', '1d', '5d', '1wk', '1mo', '3mo', '1y', '5y']
    
    rsi_int = request.rsi_interval if request.rsi_interval in valid_intervals else "1h"
    price_int = request.price_interval if request.price_interval in valid_intervals else "1h"

    data = process_stock_analysis(symbol, rsi_interval=rsi_int, price_interval=price_int)
    
    if data:
        return {"status": "success", "data": data}
    else:
        raise HTTPException(status_code=404, detail="Veri bulunamadı.")

# --- YENİDEN EKLENEN HESAP SİLME FONKSİYONU ---
@app.delete("/api/users/{user_id}")
def delete_user_account(user_id: str):
    if not db:
        raise HTTPException(status_code=500, detail="Database bağlantısı yok.")
        
    try:
        alarms_ref = db.collection('users').document(user_id).collection('alarms').stream()
        for alarm in alarms_ref:
            alarm.reference.delete()
            
        db.collection('users').document(user_id).delete()
        
        try:
            auth.delete_user(user_id)
        except Exception as auth_e:
            print(f"Auth silme atlandı (Zaten silinmiş olabilir): {auth_e}")

        print(f"🗑️ BAŞARILI: {user_id} ID'li kullanıcı ve tüm verileri evrenden silindi.")
        return {"status": "success", "message": "Hesap ve tüm veriler silindi."}
        
    except Exception as e:
        print(f"❌ Kullanıcı silme hatası: {e}")
        raise HTTPException(status_code=500, detail=f"Silme işlemi başarısız: {str(e)}")

if __name__ == "__main__":
    print("\n🚀 BORSA API (V5 - FCM Push Notification Modu)")
    uvicorn.run(app, host="0.0.0.0", port=8001)
