from ib_insync import IB
ib = IB()
ib.connect('127.0.0.1', 7497, clientId=10)
print('Connected:', ib.isConnected())
ib.disconnect()
