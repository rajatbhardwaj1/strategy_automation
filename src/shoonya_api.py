import pyotp
import time
from api_helper import ShoonyaApiPy
import utils
class ShoonyaAPI:
    def __int__(self, uid, password, api_key, totp_secret, vendor_key, imei):
        self.api = ShoonyaApiPy()
        self.api_key = api_key
        self.uid = uid
        self.password = password
        self.totp_secret = totp_secret
        self.vendor_key = vendor_key
        self.imei = imei
        self.is_logged_in = False


    def login(self):
        try:
            totp = pyotp.TOTP(self.totp_secret)
            ret = self.api.login(userid=self.uid, password=self.password, twoFA=totp.now(), vendor_code=self.vendor_key,
                            api_secret=self.api_key, imei=self.imei)
            if ret is not None:
                client_name = ret['uname']
                print(f"Logged in as Shoonya {client_name}")
            else:
                print(f"Failed to login for {self.uid}")
                utils.send_telegram_message(f"Failed to login for {self.uid}")
            if 'dmsg' in ret:
                utils.send_telegram_message(f"{self.uid}, dmsg: {ret['dmsg']}")
        except Exception as e:
            utils.send_telegram_message(f"{self.uid}, Error in function Login {e}")
            exit(0)


