#!/usr/bin/env python
# coding: utf-8

# In[ ]:


import pandas as pd
import os
import json
import requests
import struct
import math
import time
from requests.auth import HTTPBasicAuth
import numpy as np

n=100

#------parametros------
login = '62147803aae760ee0c24591d'
password = '8773c9e441f7157fb937cd4b260f2c75'
year = '2025'
no_mes = '9'
filename1="lista_devices.csv"
#---------------------

#fullpath1=os.path.join( filename1)
data_IDs = pd.read_csv(filename1)
#ID=data_IDs["Id Dispositivo"]
ID=data_IDs["IDs"]
seq_number=[]
ID_copy=[]
timestamp=[]

#for l in range (0,5):
b=[[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[],[]]
seq_number.clear()
ID_copy.clear()
timestamp.clear()
for i in range (0,len(ID)):
    filename3="https://api.sigfox.com/v2/devices/"+str(ID[i])+"/consumption/"+year+"/"+no_mes
    response=requests.get(filename3,auth=HTTPBasicAuth( login, password))
    if response.status_code==200:
        payload=response.json()['consumption']
        ID_copy.append(ID[i])
        for j in range(0,31):
            if (j<len(payload['consumptions'])):
                b[j].append(payload['consumptions'][j]['frameCount'])
                
            else:
                b[j].append('NA')
    print(i)


data2csv = {'ID':  ID_copy,
        '1' : b[0],
        '2' : b[1],
        '3' : b[2],
        '4' : b[3],
        '5' : b[4],
        '6' : b[5],
        '7' : b[6],
        '8' : b[7],
        '9' : b[8],
        '10' : b[9],
        '11' : b[10],
        '12' : b[11],
        '13' : b[12],
        '14' : b[13],
        '15' : b[14],
        '16' : b[15],
        '17' : b[16],
        '18' : b[17],
        '19' : b[18],
        '20' : b[19],
        '21' : b[20],
        '22' : b[21],
        '23' : b[22],
        '24' : b[23],
        '25' : b[24],
        '26' : b[25],
        '27' : b[26],
        '28' : b[27],
        '29' : b[28],
        '30' : b[29],
        '31' : b[30]
        }
df = pd.DataFrame(data2csv, columns = ['ID','1','2','3','4','5','6','7','8','9','10','11','12','13','14','15','16','17','18','19','20','21','22','23','24','25','26','27','28','29','30','31'])
df.to_csv('resultados_'+filename1+'_'+no_mes+'_'+year+'.csv')

print('listo')

