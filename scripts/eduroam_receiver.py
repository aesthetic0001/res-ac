import sys
check='--check' in sys.argv
import sys,binascii,subprocess,os
options=[]
directory=None
stage='initialization'
def uci(args):
    proc=subprocess.Popen(['uci']+options+args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    result=proc.communicate()[0]
    if proc.returncode:
        raise RuntimeError('UCI operation failed')
    return result
try:
    if check:
        directory='/tmp/eduroam-check-'+str(os.getpid())
        os.mkdir(directory,0o700)
        os.mkdir(directory+'/config',0o700)
        os.mkdir(directory+'/delta',0o700)
        open(directory+'/config/wireless','w').write("config wifi-iface 'eduroam'\n")
        options=['-c',directory+'/config','-P',directory+'/delta']
    print('CREDENTIALS_READY');sys.stdout.flush()
    stage='receiving'
    identity=binascii.a2b_base64(sys.stdin.readline())
    password=binascii.a2b_base64(sys.stdin.readline())
    for key,value in [('identity',identity),('password',password),('disabled','0')]:
        stage='setting-'+key
        uci(['set','wireless.eduroam.'+key+'='+value])
    stage='committing'
    uci(['commit','wireless'])
    for key,value in [('identity',identity),('password',password),('disabled','0')]:
        stage='verifying-'+key
        if uci(['get','wireless.eduroam.'+key]).rstrip('\n')!=value:
            raise RuntimeError('Saved value did not match')
    print('CREDENTIALS_SAVED');sys.stdout.flush()
except Exception as error:
    print('CREDENTIALS_FAILED:'+stage+':'+type(error).__name__);sys.stdout.flush()
finally:
    if directory:
        for child in ['config','delta']:
            for name in os.listdir(directory+'/'+child):
                os.unlink(directory+'/'+child+'/'+name)
            os.rmdir(directory+'/'+child)
        os.rmdir(directory)
