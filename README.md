dynalist-backup
--------------
Backup your http://dynalist.io data by continuously exporting it to local git repo.

Why?
----
- Get a diff view of all the changes in your account.
- Protect yourself from dynalist.io going out of business/deleting your data.
- Protect yourself from your account being stolen / locked out.
- Easily access the data locally using traditional Unix tools.
- Get access to old versions of the notes

How to use?
-----------
- check out the code
- Go to your developer page (https://dynalist.io/developer) and get your secret token.
    - Place it into ~/.config/dynalist-backup-token.txt
- Create output directory:
```
mkdir ~/.local/dynalist-data   # other locations work as well, see source
git init ~/.local/dynalist-data
```
- Run it, and make sure it works:
```
./dynalist_backup.py -v
```
- Add it to cron. You can do it as often as you want (subject to your PC's CPU usage), but the API is limited by 6 requests per minute.
```
crontab -e
```
then insert a line like below (example is to run every 30 minutes)
```
14-59/30  *  * * *    timeout 25m PATH-TO/dynalist-backup/dynalist_backup.py --commit
```

- IMPORTANT: Make some changes in web interface, wait a while, and check to make sure they appear in git,
  to make sure the entire machiney works correctly.

Note the logging is designed for "cron". Without "-v" option:

- when there are no changes, there is no output
- when there are changes, the output is useful and concise enough that it looks good(-ish..) in email


File formats
------------
- `_raw*.json` contain raw data from top-level API calls (like [file/list](https://apidocs.dynalist.io/#get-all-documents-and-folders))
- `*.c.json` contain individual records, exactly as returned by [doc/read](https://apidocs.dynalist.io/#get-content-of-a-document) api call
- `*.txt` contain text-like representation. I've tried to make it more advanced than what's offered by dynalist's "export data"
   feature, in particular, it should properly handle multi-line data and "notes"

FAQ
---
- Q: Isn't this built-in into the website?
    - A: not really:
         - There is "Export data" option, but it is one time only
         - There is "note history" option in Pro plan, but it only helps with old versions, it won't help if your account is locked out or hacked.
- Q: How do I restore it?
    - A: This is currently not implemented. You might be able to import OPML files?
    - (To elaborate, I don't anticipate ever needing to restore this into dynalist.io account):
         - If the account is locked out / hacked badly enough that I lose all the data, I'll likely want to switch to a different service anyway.
         - If the entire service goes down, there will be nowhere to restore it.
         - So your git tree contains .txt files (in case you use the "last version" snapshot directly from disk, with no UI), and "json"
           files with every possible bit of raw data in case you want to write a converter to whatever service will be next.

How to view data offline?
-------------------------
I have looked for a few common viewers, but did not explore this too deeply:

Promising:

- TreeLine support: http://treeline.bellz.org/download.html
- tines -- reads OPML just fine (so we can have console access as well)))

Others:

- "tuxcards"  https://www.tuxcards.de/ (in 2020: last version 2.2.1 from 2010)
- https://www.notecasepro.com/features.php# (commerical? -- good for format list)
- "hnb" is unmaintained, replaced with "tines"
- overview https://nochkawtf.wordpress.com/2018/07/18/moving-from-org-mode/ -- recommends "tines"
- Would be cool to export to .md, but most dialects cannot reliably support multi-paragraph deep nested lists
