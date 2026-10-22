import unittest
from unittest.mock import patch
import pymysql
from fastapi.testclient import TestClient
import main

class DatabaseHTTPTests(unittest.TestCase):
    def test_login_connection_refused(self):
        with patch('main.get_connection', side_effect=pymysql.err.OperationalError(2003,'SECRET host/password')):
            r=TestClient(main.app).post('/auth/login',json={'user_id':1})
        self.assertEqual(r.status_code,503)
        self.assertEqual(r.json()['code'],'DATABASE_UNAVAILABLE')
        self.assertNotIn('SECRET',r.text)
        self.assertTrue(r.json()['data']['retry_safe'])
    def test_unknown_write_result(self):
        import asyncio,json
        from starlette.requests import Request
        from core.database_errors import database_error_response
        req=Request({'type':'http','method':'POST','path':'/agent/confirm','headers':[]})
        r=asyncio.run(database_error_response(req,pymysql.err.OperationalError(2013,'SECRET')))
        self.assertEqual(r.status_code,503)
        self.assertFalse(json.loads(r.body)['data']['retry_safe'])
        self.assertNotIn('Retry-After',r.headers)
    def test_configuration_error_not_retryable(self):
        with patch('main.get_connection', side_effect=pymysql.err.OperationalError(1045,'SECRET')):
            r=TestClient(main.app).post('/auth/login',json={'user_id':1})
        self.assertEqual(r.status_code,500)
        self.assertNotIn('SECRET',r.text)
