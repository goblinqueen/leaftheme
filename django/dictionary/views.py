from django.shortcuts import render
from django.template import loader
from django.http import HttpResponse
from gdstorage.storage import GoogleDriveStorage

# Define Google Drive Storage
gd_storage = GoogleDriveStorage()


def index(request):
    # from . import sync_dictionary, drive
    # out = drive.main()
    gd_storage = GoogleDriveStorage()
    gd_storage.open("1aAyYM5HkboBO_QWkGba5mdnXfuClYOif")
    print('kitty')
    return HttpResponse("out")
    # template = loader.get_template("index.html")
    # return HttpResponse(template.render({}, request))
