import json
import urllib
import urllib2

class AppStoreClient(object):

    def internet_on(self):
        try:
            # check if Google is alive
            response=urllib2.urlopen('http://74.125.228.100',timeout=1)
            return True
        except urllib2.URLError as err: pass
        return False

    def download_pbw(self, uuid_str):

        id_fetch_url = 'https://dev-portal.getpebble.com/api/applications/uuid/%s' % uuid_str
        appstoreid = json.load(urllib2.urlopen(id_fetch_url))['applications'][0]['id']

        pbw_link_fetch_url = 'https://appstore-api.getpebble.com/v2/apps/id/%s' % appstoreid
        pbw_link = json.load(urllib2.urlopen(pbw_link_fetch_url))['data'][0]['latest_release']['pbw_file']

        pbw_path = "/tmp/%s.pbw" % appstoreid

        # download
        urllib.urlretrieve(pbw_link, pbw_path)
        print "Downloaded PBW for uuid %s" % uuid_str
        return pbw_path
