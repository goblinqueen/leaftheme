from flask import Flask

leaftheme = Flask(__name__)


@leaftheme.route("/")
def home_view():
    return "<h1>Kitty</h1>"
