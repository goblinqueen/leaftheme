# LeafTheme
WordTheme Dictionary sync for all types of cursed uses

Uses [WordTheme](http://wordtheme.soregainochi.com/)'s dictionary from your Google Drive if you've turned on sync in the app.
This repo is designed for Heroku deployment.

## Setup for google drive sync
You'll need to set up your google credentials (as explained [here](https://developers.google.com/identity/protocols/oauth2/web-server#creatingcred))
and set the following env variables
```
SECRET_KEY=<any string>
G_CLIENT_ID=<id from your credentials json>
G_CLIENT_SECRET=<secret from your credentials json>
```
and replace the `PROJECT_ID` constant in `leaftheme/main.py`

If you're deploying to heroku, don't forget to add env variables to app's Settings -> Config Vars

## Run locally
```
$ python wsgi.py
```

## Run locally without Google API
Look for a `WordTheme/*.wt` file in your Google Drive. 

It's a zip file that should have `dictionary.txt`. 

Extract it, put in the `leaftheme` directory and run `leaftheme/dictionary.py`, and then you can mess around in `main()` to get what you need.
