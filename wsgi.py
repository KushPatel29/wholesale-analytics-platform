from werkzeug.middleware.proxy_fix import ProxyFix
from app import create_app

app = create_app()

# Apply ProxyFix for Nginx
# x_for=1: Trusts the X-Forwarded-For header (Client IP)
# x_proto=1: Trusts the X-Forwarded-Proto header (https)
# x_host=1: Trusts the X-Forwarded-Host header
# x_prefix=1: Trusts the X-Forwarded-Prefix header (/analytics)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

if __name__ == "__main__":
    app.run()