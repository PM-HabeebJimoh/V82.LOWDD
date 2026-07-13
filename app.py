"""
V82.LOWDD Web App — Entry point.

Single broker: IG Markets. The web app auto-starts a paper trading
engine on the first request that fills orders at the live IG bid/offer.

Run:
  python3 app.py             (development)
  gunicorn ... app:app        (production)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.api.app import create_app

app = create_app()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
