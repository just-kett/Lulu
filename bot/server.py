import os
from flask import Flask, send_from_directory
from threading import Thread

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(BASE_DIR, 'docs')

app = Flask(__name__)

@app.route('/')
def home():
    if os.path.exists(os.path.join(DOCS_DIR, 'index.html')):
        return send_from_directory(DOCS_DIR, 'index.html')
    return "Discord bot is running smoothly."

@app.route('/privacy')
def privacy():
    return send_from_directory(DOCS_DIR, 'privacy.html')

@app.route('/terms')
@app.route('/tos')
def terms():
    return send_from_directory(DOCS_DIR, 'terms.html')

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run, daemon=True)
    t.start()
