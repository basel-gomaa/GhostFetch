"""Challenge hooks (demo build).

The real collector classifies CAPTCHA / verification pages and can pause for a
person to solve one. The demo never opens a browser, so there is nothing to
detect; these no-ops keep callers working.
"""


def is_captcha_page(page):
    return False


def set_default_handler(handler):
    return None


def quiet_transport_logging():
    return None
