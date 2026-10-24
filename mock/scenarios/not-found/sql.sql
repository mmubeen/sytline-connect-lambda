INSERT dbo.custaddr (cust_num, cust_seq, name, [addr##1], city, state, zip, country) VALUES
  ('C000001', 0, 'MOCK AEROSPACE LTD', '1 Example Way', 'Testville', 'CT', '06000', 'USA');
INSERT dbo.co (co_num, cust_num, cust_seq, terms_code, ship_code) VALUES ('MOCK-OLD-1', 'C000001', 0, 'N30', 'FOB');
