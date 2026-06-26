<!-- converted from BSL_Agent_Mock_Customers_TestScenarios.docx -->

BSL Voice Agent — Mock Customer Data & Test Scenarios
Demo voice agent for Bank of Sri Lanka (BSL). Six mock accounts across five customers (Nimal Perera holds two — a personal Current account and a business account). English-only flow. Verification is a fixed 3-field check: NIC, DOB, and Mother's Maiden Name (matched phonetically via Double Metaphone). All amounts in LKR.
# How to Use This Document
- Pick a test account below.
- Dial the BSL Twilio number (+19476669436).
- State the account number when asked (Step 1) — the agent reads back the last 4 digits.
- Provide NIC, DOB, and Mother's Maiden Name together in one turn (Step 2).
- Ask for any of: Account Balance, Block Debit Card, Account Details, Recent Transactions, Loans, Standing Orders.
- After serving, the agent loops back with "Anything else?" — re-verification is NOT required for the same account in the same call.
# Multi-Account Holders
- Nimal Perera → 1042-8837-9201, 1098-5541-3367
# Test Accounts
## 1. Nimal Perera  (Current Account)
### Account & Identity

### Account Summary

### Debit Card

### Loans

### Standing Orders

### Transactions (Statement Period)

## 2. Nimal Perera — Perera Tech Solutions (Pvt) Ltd  (Business Current Account)
### Account & Identity

### Account Summary

### Debit Card

### Loans

### Standing Orders

### Transactions (Statement Period)

## 3. Dilani Wijesinghe  (Savings Account)
### Account & Identity

### Account Summary

### Debit Card

### Loans
None.
### Standing Orders

### Transactions (Statement Period)

## 4. Ruwan Bandara  (Current Account)
### Account & Identity

### Account Summary

### Debit Card

### Loans

### Standing Orders

### Transactions (Statement Period)

## 5. Amara Dissanayake — Horizon Trading (Pvt) Ltd  (Business Current Account)
### Account & Identity

### Account Summary

### Debit Card

### Loans

### Standing Orders

### Transactions (Statement Period)

## 6. Sahan Mendis — ByteNest Solutions (Pvt) Ltd  (Business Current Account)
### Account & Identity

### Account Summary

### Debit Card

### Loans
None.
### Standing Orders

### Transactions (Statement Period)

# Suggested Test Scenarios
S1 — Personal Account Balance (happy path)
Caller: Nimal Perera. Account: 1042-8837-9201 (ends in 9201). Asks for current balance. Verify with NIC 901234567V, DOB 12 April 1990, Mother's Maiden 'Jayasinghe'. Expected: agent reads back '...ending in 9201', confirms identity, then speaks 'three hundred fifty-four thousand, two hundred seventeen rupees'.
S2 — Business Account Details (multi-account customer)
Same caller (Nimal Perera) but states the business account 1098-5541-3367 (ends in 3367). Verification identical. Expected: agent uses the BUSINESS template ('Perera Tech Solutions (Pvt) Ltd', closing balance LKR 1,940,955.00) and the 'primary account holder' wording variants.
S3 — Block Debit Card
Caller: Dilani Wijesinghe, account 2087-4412-6654 (ends in 6654). Asks to block her card. Verify with NIC 198876543210, DOB 28 November 1988, Mother's Maiden 'Fernando'. Expected: card status flipped to Blocked for this session ONLY (no JSON mutation, next caller still sees Active).
S4 — Recent Transactions readout
Caller: Ruwan Bandara, account 3054-9960-1138 (ends in 1138). Asks for last 5 transactions. Verify NIC 197523456789, DOB 07 September 1975, Mother's Maiden 'Seneviratne'. Expected: most-recent-first ordering, amounts spoken in natural LKR.
S5 — Loans inquiry (large business account)
Caller: Amara Dissanayake (Horizon Trading), account 4901-2233-4477 (ends in 4477). Asks about loan facility. Verify NIC 198245678901, DOB 15 June 1982, Mother's Maiden 'Rajapaksa'. Expected: 'Business Overdraft Facility OD-2022-00890, original LKR 3,000,000, currently unused.'
S6 — Standing Orders (SME with USD subscriptions)
Caller: Sahan Mendis (ByteNest Solutions), account 5673-8800-2295 (ends in 2295). Asks for standing orders. Verify NIC 199312345670, DOB 30 March 1993, Mother's Maiden 'Gunawardena'. Expected: agent lists Office Rent Colombo 07 (LKR 85,000) and AWS Cloud Services (LKR 94,550).
S7 — Phonetic Mother's Maiden Name (STT noise)
Use any account. When asked for mother's maiden name, say it deliberately mangled (e.g. 'giant singer' or 'Jinger' for 'Jayasinghe', 'F-air-nan-do' for 'Fernando'). Expected: Double Metaphone still passes — NIC and DOB stay strict, so phonetic leniency is safe.
S8 — Account-number-not-found does NOT burn an attempt
Verify against a real account, but at Step 1 first state a non-existent account (e.g. 9999-9999-9999). Expected: agent apologises and re-asks ONLY the account number; verification_attempts counter does not increment. Then state the real account and proceed normally.
S9 — Three failed verifications → live-agent handoff
Pick any account. Give intentionally wrong NIC three times. Expected: after 3rd failure agent speaks 'I'm afraid I'm unable to verify your identity at this stage... please hold.' Call STAYS OPEN (no Twilio Dial); caller hangs up.
S10 — Loop within the same call (no re-verify)
Verify against 1042-8837-9201 and get a balance. When agent asks 'Anything else?', ask for recent transactions on the SAME account. Expected: agent serves immediately, no re-verification. Now ask about a DIFFERENT account (1098-5541-3367) — agent treats as fresh Step 1 and re-verifies.
S11 — Last-4 only account lookup (STT drops a 4-digit group)
Say only 'ending in 9201' or just '9201' at Step 1. Expected: _normalize_account_no falls back to last-4 lookup, agent confirms '...ending in 9201' and proceeds.
S12 — NIC V/B leniency
Use account 1042-8837-9201. Say NIC as '901234567B' instead of '901234567V'. Expected: verification passes (V/B trailing toggle is tolerated by mock_db.verify_identity).
| Account Number | 1042-8837-9201 |
| --- | --- |
| Last 4 Digits | 9201 |
| Account Holder | Nimal Perera |
| Company Name | — |
| Account Type | Current Account |
| Business Account? | No |
| Branch | Nugegoda |
| Opened Date | 14 March 2019 |
| NIC (verification) | 901234567V |
| Date of Birth (verification) | 12 April 1990 |
| Mother's Maiden Name (verification) | Jayasinghe |
| Statement Period | 01 April 2026 — 30 April 2026 |
| --- | --- |
| Opening Balance | LKR 285,000.00 |
| Closing Balance | LKR 354,217.00 |
| Internet Banking | Yes |
| Mobile Banking | Yes |
| Registered Mobile | 0771234567 |
| Registered Email | nimal.perera@gmail.com |
| Card Number (masked) | **** **** **** 4412 |
| --- | --- |
| Card Type | Visa Debit |
| Expiry | 08/2027 |
| Status | Active |
| Daily Limit | LKR 100,000.00 |
| Product | Reference | Original | Outstanding | Monthly | Next Due |
| --- | --- | --- | --- | --- | --- |
| Personal Loan | PL-2023-00441 | LKR 500,000.00 | LKR 187,500.00 | LKR 15,625.00 | 05 May 2026 |
| Payee | Amount | Execution |
| --- | --- | --- |
| Dialog Axiata | LKR 1,499.00 | 1st of each month |
| Sanasa Life Insurance | LKR 3,200.00 | 5th of each month |
| Date | Description | Dr/Cr | Amount | Balance |
| --- | --- | --- | --- | --- |
| 01 Apr | Opening Balance | — | LKR 285,000.00 | LKR 285,000.00 |
| 07 Apr | POS — Keells Super Nugegoda | DR | LKR 3,450.00 | LKR 281,550.00 |
| 08 Apr | ATM Withdrawal — Nugegoda Branch | DR | LKR 10,000.00 | LKR 271,550.00 |
| 09 Apr | Bill Pay — Dialog Axiata (SO) | DR | LKR 1,499.00 | LKR 270,051.00 |
| 10 Apr | Online Transfer Out — Sampath 7723 | DR | LKR 15,000.00 | LKR 255,051.00 |
| 11 Apr | POS — Laugfs Petrol, Nugegoda | DR | LKR 4,200.00 | LKR 250,851.00 |
| 06 Apr | Standing Order — Sanasa Life Ins | DR | LKR 3,200.00 | LKR 247,651.00 |
| 07 Apr | POS — Odel, Colombo 03 | DR | LKR 7,800.00 | LKR 239,851.00 |
| 08 Apr | ATM Withdrawal — Maharagama | DR | LKR 5,000.00 | LKR 234,851.00 |
| 09 Apr | Bill Pay — CEB (Ceylon Electricity) | DR | LKR 3,870.00 | LKR 230,981.00 |
| 10 Apr | Loan Repayment — PL-2023-00441 | DR | LKR 15,625.00 | LKR 215,356.00 |
| 11 Apr | POS — Cargills Food City, Nawala | DR | LKR 2,180.00 | LKR 213,176.00 |
| 12 Apr | Online Transfer In — HNB 4491 | CR | LKR 25,000.00 | LKR 238,176.00 |
| 13 Apr | POS — Pizza Hut, Nugegoda | DR | LKR 2,450.00 | LKR 235,726.00 |
| 14 Apr | ATM Withdrawal — Nugegoda Branch | DR | LKR 10,000.00 | LKR 225,726.00 |
| 15 Apr | POS — Softlogic, One Galle Face | DR | LKR 12,500.00 | LKR 213,226.00 |
| 16 Apr | Bill Pay — SLT Broadband | DR | LKR 2,199.00 | LKR 211,027.00 |
| 17 Apr | Online Transfer Out — Commercial 8812 | DR | LKR 8,000.00 | LKR 203,027.00 |
| 18 Apr | POS — Keells Super, Rajagiriya | DR | LKR 4,750.00 | LKR 198,277.00 |
| 19 Apr | ATM Withdrawal — Rajagiriya Branch | DR | LKR 10,000.00 | LKR 188,277.00 |
| 20 Apr | POS — Laugfs Petrol, Rajagiriya | DR | LKR 3,800.00 | LKR 184,477.00 |
| 21 Apr | Online Transfer In — BOC 3302 | CR | LKR 50,000.00 | LKR 234,477.00 |
| 22 Apr | POS — Bata, Liberty Plaza | DR | LKR 3,490.00 | LKR 230,987.00 |
| 23 Apr | Salary Credit — ABC Exports (Pvt) Ltd | CR | LKR 185,000.00 | LKR 415,987.00 |
| 24 Apr | POS — Cargills, Nawala | DR | LKR 3,120.00 | LKR 412,867.00 |
| 25 Apr | Online Transfer Out — Sampath 7723 | DR | LKR 25,000.00 | LKR 387,867.00 |
| 26 Apr | ATM Withdrawal — Nugegoda Branch | DR | LKR 20,000.00 | LKR 367,867.00 |
| 27 Apr | POS — Keells Super, Nugegoda | DR | LKR 4,350.00 | LKR 363,517.00 |
| 28 Apr | Bill Pay — Lanka Hospitals OPD | DR | LKR 5,500.00 | LKR 358,017.00 |
| 29 Apr | POS — Laugfs Petrol, Nawala | DR | LKR 3,800.00 | LKR 354,217.00 |
| 30 Apr | Closing Balance | — | — | LKR 354,217.00 |
| Account Number | 1098-5541-3367 |
| --- | --- |
| Last 4 Digits | 3367 |
| Account Holder | Nimal Perera |
| Company Name | Perera Tech Solutions (Pvt) Ltd |
| Account Type | Business Current Account |
| Business Account? | Yes |
| Branch | Nugegoda |
| Opened Date | 02 June 2022 |
| NIC (verification) | 901234567V |
| Date of Birth (verification) | 12 April 1990 |
| Mother's Maiden Name (verification) | Jayasinghe |
| Statement Period | 01 April 2026 — 30 April 2026 |
| --- | --- |
| Opening Balance | LKR 1,250,000.00 |
| Closing Balance | LKR 1,940,955.00 |
| Internet Banking | Yes |
| Mobile Banking | Yes |
| Registered Mobile | 0771234567 |
| Registered Email | accounts@pereratech.lk |
| Card Number (masked) | **** **** **** 6618 |
| --- | --- |
| Card Type | Visa Business Debit |
| Expiry | 05/2028 |
| Status | Active |
| Daily Limit | LKR 300,000.00 |
| Product | Reference | Original | Outstanding | Monthly | Next Due |
| --- | --- | --- | --- | --- | --- |
| Business Term Loan | BTL-2023-00229 | LKR 2,000,000.00 | LKR 1,340,000.00 | LKR 55,000.00 | 10 May 2026 |
| Payee | Amount | Execution |
| --- | --- | --- |
| Office Rent — Nugegoda | LKR 65,000.00 | 1st of each month |
| Dialog Corporate | LKR 8,500.00 | 5th of each month |
| AWS Cloud Services (USD 180 est.) | LKR 54,900.00 | 15th of each month |
| Date | Description | Dr/Cr | Amount | Balance |
| --- | --- | --- | --- | --- |
| 01 Apr | Opening Balance | — | LKR 1,250,000.00 | LKR 1,250,000.00 |
| 07 Apr | Client Payment — Virtusa (Pvt) Ltd | CR | LKR 420,000.00 | LKR 1,670,000.00 |
| 08 Apr | Office Rent — Nugegoda (SO) | DR | LKR 65,000.00 | LKR 1,605,000.00 |
| 09 Apr | Payroll — March 2026 (Partial) | DR | LKR 180,000.00 | LKR 1,425,000.00 |
| 10 Apr | Payroll — March 2026 (Final) | DR | LKR 145,000.00 | LKR 1,280,000.00 |
| 11 Apr | Dialog Corporate Bill (SO) | DR | LKR 8,500.00 | LKR 1,271,500.00 |
| 06 Apr | POS — Laptop Parts, Borella | DR | LKR 38,000.00 | LKR 1,233,500.00 |
| 07 Apr | Client Payment — HNB IT Dept | CR | LKR 325,000.00 | LKR 1,558,500.00 |
| 08 Apr | Supplier Payment — SLT Enterprise | DR | LKR 24,000.00 | LKR 1,534,500.00 |
| 09 Apr | POS — Keells Horton Place (Office) | DR | LKR 9,200.00 | LKR 1,525,300.00 |
| 10 Apr | ATM Withdrawal — Nugegoda Branch | DR | LKR 50,000.00 | LKR 1,475,300.00 |
| 11 Apr | Business Term Loan EMI — BTL-2023-00229 | DR | LKR 55,000.00 | LKR 1,420,300.00 |
| 12 Apr | POS — Office Supplies, Borella | DR | LKR 12,400.00 | LKR 1,407,900.00 |
| 13 Apr | Client Payment — Dialog Axiata IT | CR | LKR 280,000.00 | LKR 1,687,900.00 |
| 14 Apr | Payroll Advance — April | DR | LKR 70,000.00 | LKR 1,617,900.00 |
| 15 Apr | POS — Adobe Creative Cloud (USD 89) | DR | LKR 27,145.00 | LKR 1,590,755.00 |
| 16 Apr | Bill Pay — SLT Corporate Broadband | DR | LKR 6,500.00 | LKR 1,584,255.00 |
| 17 Apr | POS — Softlogic (Office Equipment) | DR | LKR 44,000.00 | LKR 1,540,255.00 |
| 18 Apr | Online Transfer Out — Sampath 9912 | DR | LKR 100,000.00 | LKR 1,440,255.00 |
| 19 Apr | Client Payment — BOC IT Services | CR | LKR 510,000.00 | LKR 1,950,255.00 |
| 20 Apr | AWS Cloud Services (SO — USD 180) | DR | LKR 54,900.00 | LKR 1,895,355.00 |
| 21 Apr | POS — Dialog Business Centre | DR | LKR 11,000.00 | LKR 1,884,355.00 |
| 22 Apr | Supplier Payment — Lanka Printers | DR | LKR 28,500.00 | LKR 1,855,855.00 |
| 23 Apr | Payroll — April 2026 (Final) | DR | LKR 325,000.00 | LKR 1,530,855.00 |
| 24 Apr | Client Payment — Sampath Bank IT | CR | LKR 390,000.00 | LKR 1,920,855.00 |
| 25 Apr | POS — Laptop Battery, Borella | DR | LKR 6,800.00 | LKR 1,914,055.00 |
| 26 Apr | Bill Pay — CEB Commercial | DR | LKR 14,200.00 | LKR 1,899,855.00 |
| 27 Apr | Online Transfer Out — BOC 7712 | DR | LKR 200,000.00 | LKR 1,699,855.00 |
| 28 Apr | POS — Prima Ceylon (Office Supplies) | DR | LKR 8,900.00 | LKR 1,690,955.00 |
| 29 Apr | Client Payment — Hatton National IT | CR | LKR 250,000.00 | LKR 1,940,955.00 |
| 30 Apr | Closing Balance | — | — | LKR 1,940,955.00 |
| Account Number | 2087-4412-6654 |
| --- | --- |
| Last 4 Digits | 6654 |
| Account Holder | Dilani Wijesinghe |
| Company Name | — |
| Account Type | Savings Account |
| Business Account? | No |
| Branch | Kandy |
| Opened Date | 03 July 2021 |
| NIC (verification) | 198876543210 |
| Date of Birth (verification) | 28 November 1988 |
| Mother's Maiden Name (verification) | Fernando |
| Statement Period | 01 April 2026 — 30 April 2026 |
| --- | --- |
| Opening Balance | LKR 115,000.00 |
| Closing Balance | LKR 106,502.00 |
| Internet Banking | Yes |
| Mobile Banking | No |
| Registered Mobile | 0772345678 |
| Registered Email | dilani.w@yahoo.com |
| Card Number (masked) | **** **** **** 7731 |
| --- | --- |
| Card Type | Mastercard Debit |
| Expiry | 11/2026 |
| Status | Active |
| Daily Limit | LKR 50,000.00 |
| Payee | Amount | Execution |
| --- | --- | --- |
| Dialog Axiata | LKR 1,099.00 | 3rd of each month |
| LOLC Life Insurance | LKR 2,500.00 | 10th of each month |
| Date | Description | Dr/Cr | Amount | Balance |
| --- | --- | --- | --- | --- |
| 01 Apr | Opening Balance | — | LKR 115,000.00 | LKR 115,000.00 |
| 07 Apr | POS — Cargills Kandy City Centre | DR | LKR 2,890.00 | LKR 112,110.00 |
| 08 Apr | ATM Withdrawal — Kandy Branch | DR | LKR 10,000.00 | LKR 102,110.00 |
| 09 Apr | POS — Laugfs Petrol, Kandy | DR | LKR 3,200.00 | LKR 98,910.00 |
| 10 Apr | Bill Pay — SLT Broadband Kandy | DR | LKR 1,699.00 | LKR 97,211.00 |
| 06 Apr | Online Transfer Out — Peoples 9901 | DR | LKR 5,000.00 | LKR 92,211.00 |
| 07 Apr | POS — Arpico, Kandy | DR | LKR 4,150.00 | LKR 88,061.00 |
| 08 Apr | ATM Withdrawal — Kandy City Centre | DR | LKR 5,000.00 | LKR 83,061.00 |
| 09 Apr | Bill Pay — Dialog Axiata (SO) | DR | LKR 1,099.00 | LKR 81,962.00 |
| 10 Apr | POS — KFC, Kandy | DR | LKR 1,450.00 | LKR 80,512.00 |
| 11 Apr | Online Transfer In — Commercial 5521 | CR | LKR 20,000.00 | LKR 100,512.00 |
| 12 Apr | POS — Keells, Kandy City Centre | DR | LKR 3,780.00 | LKR 96,732.00 |
| 13 Apr | ATM Withdrawal — Peradeniya Branch | DR | LKR 5,000.00 | LKR 91,732.00 |
| 14 Apr | POS — Odel, Kandy | DR | LKR 6,200.00 | LKR 85,532.00 |
| 15 Apr | Standing Order — LOLC Life Ins | DR | LKR 2,500.00 | LKR 83,032.00 |
| 16 Apr | Bill Pay — LECO (Electricity) | DR | LKR 2,340.00 | LKR 80,692.00 |
| 17 Apr | POS — Cargills, Kandy | DR | LKR 1,990.00 | LKR 78,702.00 |
| 18 Apr | ATM Withdrawal — Kandy Branch | DR | LKR 10,000.00 | LKR 68,702.00 |
| 19 Apr | Online Transfer In — BOC 7712 | CR | LKR 15,000.00 | LKR 83,702.00 |
| 20 Apr | POS — Laugfs Petrol, Peradeniya | DR | LKR 2,900.00 | LKR 80,802.00 |
| 21 Apr | POS — Kandy City Centre Pharmacy | DR | LKR 1,780.00 | LKR 79,022.00 |
| 22 Apr | ATM Withdrawal — Kandy City Centre | DR | LKR 5,000.00 | LKR 74,022.00 |
| 23 Apr | Salary Credit — Ministry of Education | CR | LKR 72,000.00 | LKR 146,022.00 |
| 24 Apr | POS — Cargills, Kandy City | DR | LKR 2,450.00 | LKR 143,572.00 |
| 25 Apr | Online Transfer Out — Peoples 9901 | DR | LKR 10,000.00 | LKR 133,572.00 |
| 26 Apr | Bill Pay — Dialog Axiata | DR | LKR 1,490.00 | LKR 132,082.00 |
| 27 Apr | ATM Withdrawal — Kandy Branch | DR | LKR 20,000.00 | LKR 112,082.00 |
| 28 Apr | POS — Cargills, Kandy | DR | LKR 2,180.00 | LKR 109,902.00 |
| 29 Apr | POS — Arpico, Kandy | DR | LKR 3,400.00 | LKR 106,502.00 |
| 30 Apr | Closing Balance | — | — | LKR 106,502.00 |
| Account Number | 3054-9960-1138 |
| --- | --- |
| Last 4 Digits | 1138 |
| Account Holder | Ruwan Bandara |
| Company Name | — |
| Account Type | Current Account |
| Business Account? | No |
| Branch | Galle |
| Opened Date | 22 January 2017 |
| NIC (verification) | 197523456789 |
| Date of Birth (verification) | 07 September 1975 |
| Mother's Maiden Name (verification) | Seneviratne |
| Statement Period | 01 April 2026 — 30 April 2026 |
| --- | --- |
| Opening Balance | LKR 950,000.00 |
| Closing Balance | LKR 1,084,231.00 |
| Internet Banking | Yes |
| Mobile Banking | Yes |
| Registered Mobile | 0773456789 |
| Registered Email | ruwan.bandara@sltnet.lk |
| Card Number (masked) | **** **** **** 2209 |
| --- | --- |
| Card Type | Visa Debit |
| Expiry | 03/2028 |
| Status | Active |
| Daily Limit | LKR 250,000.00 |
| Product | Reference | Original | Outstanding | Monthly | Next Due |
| --- | --- | --- | --- | --- | --- |
| Housing Loan | HL-2018-00112 | LKR 8,500,000.00 | LKR 4,230,000.00 | LKR 95,000.00 | 01 May 2026 |
| Payee | Amount | Execution |
| --- | --- | --- |
| LECO (Electricity) | LKR 6,500.00 | 2nd of each month |
| AIA Life Insurance | LKR 12,000.00 | 7th of each month |
| Date | Description | Dr/Cr | Amount | Balance |
| --- | --- | --- | --- | --- |
| 01 Apr | Opening Balance | — | LKR 950,000.00 | LKR 950,000.00 |
| 07 Apr | Supplier Payment — Lanka Tiles | DR | LKR 87,500.00 | LKR 862,500.00 |
| 08 Apr | ATM Withdrawal — Galle Fort Branch | DR | LKR 50,000.00 | LKR 812,500.00 |
| 09 Apr | POS — Cargills, Galle Town | DR | LKR 8,450.00 | LKR 804,050.00 |
| 10 Apr | Bill Pay — LECO Industrial (SO) | DR | LKR 6,500.00 | LKR 797,550.00 |
| 11 Apr | Online Transfer Out — HNB 2281 | DR | LKR 100,000.00 | LKR 697,550.00 |
| 06 Apr | POS — Laugfs Petrol, Galle | DR | LKR 7,200.00 | LKR 690,350.00 |
| 07 Apr | Standing Order — AIA Life Ins | DR | LKR 12,000.00 | LKR 678,350.00 |
| 08 Apr | ATM Withdrawal — Galle Branch | DR | LKR 50,000.00 | LKR 628,350.00 |
| 09 Apr | POS — Odel, Majestic City Colombo | DR | LKR 22,500.00 | LKR 605,850.00 |
| 10 Apr | Online Transfer Out — Peoples 4490 | DR | LKR 200,000.00 | LKR 405,850.00 |
| 11 Apr | POS — Keells, Galle | DR | LKR 5,900.00 | LKR 399,950.00 |
| 12 Apr | Housing Loan EMI — HL-2018-00112 | DR | LKR 95,000.00 | LKR 304,950.00 |
| 13 Apr | ATM Withdrawal — Hikkaduwa Branch | DR | LKR 30,000.00 | LKR 274,950.00 |
| 14 Apr | Bill Pay — SLT Broadband | DR | LKR 3,499.00 | LKR 271,451.00 |
| 15 Apr | POS — Lanka Tiles, Galle | DR | LKR 45,000.00 | LKR 226,451.00 |
| 16 Apr | Online Transfer In — Commercial 7712 | CR | LKR 500,000.00 | LKR 726,451.00 |
| 17 Apr | POS — Arpico, Galle | DR | LKR 12,000.00 | LKR 714,451.00 |
| 18 Apr | ATM Withdrawal — Galle Fort | DR | LKR 50,000.00 | LKR 664,451.00 |
| 19 Apr | Bill Pay — Dialog (Mobile) | DR | LKR 4,500.00 | LKR 659,951.00 |
| 20 Apr | POS — Softlogic, Galle | DR | LKR 18,000.00 | LKR 641,951.00 |
| 21 Apr | Online Transfer Out — Sampath 3312 | DR | LKR 100,000.00 | LKR 541,951.00 |
| 22 Apr | POS — Cargills, Galle Town | DR | LKR 7,200.00 | LKR 534,751.00 |
| 23 Apr | ATM Withdrawal — Galle Branch | DR | LKR 50,000.00 | LKR 484,751.00 |
| 24 Apr | Business Income Credit | CR | LKR 950,000.00 | LKR 1,434,751.00 |
| 25 Apr | POS — Laugfs, Galle | DR | LKR 6,800.00 | LKR 1,427,951.00 |
| 26 Apr | Online Transfer Out — BOC 9921 | DR | LKR 200,000.00 | LKR 1,227,951.00 |
| 27 Apr | POS — Lanka Tiles, Galle | DR | LKR 87,500.00 | LKR 1,140,451.00 |
| 28 Apr | ATM Withdrawal — Galle Fort Branch | DR | LKR 50,000.00 | LKR 1,090,451.00 |
| 29 Apr | Bill Pay — CEB | DR | LKR 6,220.00 | LKR 1,084,231.00 |
| 30 Apr | Closing Balance | — | — | LKR 1,084,231.00 |
| Account Number | 4901-2233-4477 |
| --- | --- |
| Last 4 Digits | 4477 |
| Account Holder | Amara Dissanayake |
| Company Name | Horizon Trading (Pvt) Ltd |
| Account Type | Business Current Account |
| Business Account? | Yes |
| Branch | Colombo 03 |
| Opened Date | 11 August 2020 |
| NIC (verification) | 198245678901 |
| Date of Birth (verification) | 15 June 1982 |
| Mother's Maiden Name (verification) | Rajapaksa |
| Statement Period | 01 April 2026 — 30 April 2026 |
| --- | --- |
| Opening Balance | LKR 4,800,000.00 |
| Closing Balance | LKR 5,720,400.00 |
| Internet Banking | Yes |
| Mobile Banking | Yes |
| Registered Mobile | 0774567890 |
| Registered Email | accounts@horizontrading.lk |
| Card Number (masked) | **** **** **** 8823 |
| --- | --- |
| Card Type | Visa Business Debit |
| Expiry | 06/2027 |
| Status | Active |
| Daily Limit | LKR 500,000.00 |
| Product | Reference | Original | Outstanding | Monthly | Next Due |
| --- | --- | --- | --- | --- | --- |
| Business Overdraft Facility | OD-2022-00890 | LKR 3,000,000.00 | LKR 0.00 | — | — |
| Payee | Amount | Execution |
| --- | --- | --- |
| CEB Industrial Tariff | LKR 94,300.00 | 10th of each month |
| Warehouse Rent — Colombo 15 | LKR 220,000.00 | 1st of each month |
| Date | Description | Dr/Cr | Amount | Balance |
| --- | --- | --- | --- | --- |
| 01 Apr | Opening Balance | — | LKR 4,800,000.00 | LKR 4,800,000.00 |
| 07 Apr | Supplier Payment — Brandix Apparel | DR | LKR 380,000.00 | LKR 4,420,000.00 |
| 08 Apr | Payroll — March 2026 (Partial) | DR | LKR 620,000.00 | LKR 3,800,000.00 |
| 09 Apr | Payroll — March 2026 (Final) | DR | LKR 630,000.00 | LKR 3,170,000.00 |
| 10 Apr | Rent — Warehouse Colombo 15 (SO) | DR | LKR 220,000.00 | LKR 2,950,000.00 |
| 11 Apr | Utility — CEB Industrial Tariff | DR | LKR 94,300.00 | LKR 2,855,700.00 |
| 06 Apr | Export Receipt — EUR 12,400 @ 360 | CR | LKR 4,464,000.00 | LKR 7,319,700.00 |
| 07 Apr | Supplier Payment — Mas Fabrics | DR | LKR 480,000.00 | LKR 6,839,700.00 |
| 08 Apr | Online Transfer Out — HNB 9910 | DR | LKR 200,000.00 | LKR 6,639,700.00 |
| 09 Apr | POS — Prima Ceylon (Raw Materials) | DR | LKR 125,000.00 | LKR 6,514,700.00 |
| 10 Apr | Supplier Payment — DIMO Parts | DR | LKR 89,000.00 | LKR 6,425,700.00 |
| 11 Apr | Bill Pay — Dialog Corporate | DR | LKR 22,000.00 | LKR 6,403,700.00 |
| 12 Apr | POS — Laugfs Fuel Depot | DR | LKR 38,000.00 | LKR 6,365,700.00 |
| 13 Apr | Supplier Payment — Lanka Harness | DR | LKR 267,000.00 | LKR 6,098,700.00 |
| 14 Apr | Export Receipt — USD 8,200 @ 305 | CR | LKR 2,501,000.00 | LKR 8,599,700.00 |
| 15 Apr | Warehouse Rent — SO | DR | LKR 220,000.00 | LKR 8,379,700.00 |
| 16 Apr | CEB Industrial Tariff — SO | DR | LKR 94,300.00 | LKR 8,285,400.00 |
| 17 Apr | Supplier Payment — Brandix Apparel | DR | LKR 410,000.00 | LKR 7,875,400.00 |
| 18 Apr | Payroll Advance — April 2026 | DR | LKR 300,000.00 | LKR 7,575,400.00 |
| 19 Apr | POS — Keells Horton Place (Office) | DR | LKR 14,500.00 | LKR 7,560,900.00 |
| 20 Apr | Online Transfer Out — Sampath 7723 | DR | LKR 1,250,000.00 | LKR 6,310,900.00 |
| 21 Apr | POS — Softlogic (Office Equipment) | DR | LKR 88,000.00 | LKR 6,222,900.00 |
| 22 Apr | Supplier Payment — Mas Fabrics | DR | LKR 480,000.00 | LKR 5,742,900.00 |
| 23 Apr | Payroll — April 2026 (Final) | DR | LKR 950,000.00 | LKR 4,792,900.00 |
| 24 Apr | Export Receipt — EUR 9,800 @ 360 | CR | LKR 3,528,000.00 | LKR 8,320,900.00 |
| 25 Apr | Supplier Payment — DIMO Parts | DR | LKR 102,000.00 | LKR 8,218,900.00 |
| 26 Apr | Bill Pay — SLT Corporate Broadband | DR | LKR 18,500.00 | LKR 8,200,400.00 |
| 27 Apr | POS — Prima Ceylon (Materials) | DR | LKR 178,000.00 | LKR 8,022,400.00 |
| 28 Apr | Online Transfer Out — BOC 4412 | DR | LKR 2,000,000.00 | LKR 6,022,400.00 |
| 29 Apr | Supplier Payment — Lanka Harness | DR | LKR 302,000.00 | LKR 5,720,400.00 |
| 30 Apr | Closing Balance | — | — | LKR 5,720,400.00 |
| Account Number | 5673-8800-2295 |
| --- | --- |
| Last 4 Digits | 2295 |
| Account Holder | Sahan Mendis |
| Company Name | ByteNest Solutions (Pvt) Ltd |
| Account Type | Business Current Account |
| Business Account? | Yes |
| Branch | Colombo 07 |
| Opened Date | 05 February 2023 |
| NIC (verification) | 199312345670 |
| Date of Birth (verification) | 30 March 1993 |
| Mother's Maiden Name (verification) | Gunawardena |
| Statement Period | 01 April 2026 — 30 April 2026 |
| --- | --- |
| Opening Balance | LKR 680,000.00 |
| Closing Balance | LKR 927,607.00 |
| Internet Banking | Yes |
| Mobile Banking | Yes |
| Registered Mobile | 0775678901 |
| Registered Email | finance@bytenest.lk |
| Card Number (masked) | **** **** **** 5509 |
| --- | --- |
| Card Type | Mastercard Business Debit |
| Expiry | 01/2027 |
| Status | Active |
| Daily Limit | LKR 300,000.00 |
| Payee | Amount | Execution |
| --- | --- | --- |
| Office Rent — Colombo 07 | LKR 85,000.00 | 1st of each month |
| AWS Cloud Services (USD 310 est.) | LKR 94,550.00 | 15th of each month |
| Date | Description | Dr/Cr | Amount | Balance |
| --- | --- | --- | --- | --- |
| 01 Apr | Opening Balance | — | LKR 680,000.00 | LKR 680,000.00 |
| 07 Apr | Client Payment — Hatton National Bank | CR | LKR 330,000.00 | LKR 1,010,000.00 |
| 08 Apr | Office Rent — Colombo 07 (SO) | DR | LKR 85,000.00 | LKR 925,000.00 |
| 09 Apr | Payroll — March 2026 (Partial) | DR | LKR 180,000.00 | LKR 745,000.00 |
| 10 Apr | Payroll — March 2026 (Final) | DR | LKR 140,000.00 | LKR 605,000.00 |
| 11 Apr | POS — Dialog Business Centre | DR | LKR 18,500.00 | LKR 586,500.00 |
| 06 Apr | Client Payment — Dialog Axiata | CR | LKR 220,000.00 | LKR 806,500.00 |
| 07 Apr | POS — Adobe Creative Cloud (USD 89) | DR | LKR 27,145.00 | LKR 779,355.00 |
| 08 Apr | POS — Keells, Colombo 07 | DR | LKR 6,200.00 | LKR 773,155.00 |
| 09 Apr | Bill Pay — SLT Office Broadband | DR | LKR 4,999.00 | LKR 768,156.00 |
| 10 Apr | ATM Withdrawal — Colombo 07 Branch | DR | LKR 30,000.00 | LKR 738,156.00 |
| 11 Apr | POS — Laptop Parts, Borella | DR | LKR 42,000.00 | LKR 696,156.00 |
| 12 Apr | Online Transfer Out — Sampath 8812 | DR | LKR 50,000.00 | LKR 646,156.00 |
| 13 Apr | Client Payment — Virtusa (Pvt) Ltd | CR | LKR 450,000.00 | LKR 1,096,156.00 |
| 14 Apr | Payroll Advance — April | DR | LKR 80,000.00 | LKR 1,016,156.00 |
| 15 Apr | Bill Pay — Dialog Corporate | DR | LKR 12,400.00 | LKR 1,003,756.00 |
| 16 Apr | POS — HubSpot Subscription (USD 95) | DR | LKR 28,975.00 | LKR 974,781.00 |
| 17 Apr | POS — Cargills, Colombo 07 | DR | LKR 5,400.00 | LKR 969,381.00 |
| 18 Apr | ATM Withdrawal — Colombo 07 Branch | DR | LKR 25,000.00 | LKR 944,381.00 |
| 19 Apr | POS — Office Supplies, Borella | DR | LKR 8,900.00 | LKR 935,481.00 |
| 20 Apr | AWS Cloud Services (SO — USD 310) | DR | LKR 94,550.00 | LKR 840,931.00 |
| 21 Apr | POS — Dialog Business Centre | DR | LKR 15,200.00 | LKR 825,731.00 |
| 22 Apr | Online Transfer Out — BOC 5512 | DR | LKR 100,000.00 | LKR 725,731.00 |
| 23 Apr | Payroll — April 2026 (Final) | DR | LKR 175,000.00 | LKR 550,731.00 |
| 24 Apr | Client Payment — Lanka Hospitals | CR | LKR 550,000.00 | LKR 1,100,731.00 |
| 25 Apr | POS — Adobe Subscription (USD 55) | DR | LKR 16,775.00 | LKR 1,083,956.00 |
| 26 Apr | Bill Pay — SLT Broadband | DR | LKR 4,999.00 | LKR 1,078,957.00 |
| 27 Apr | POS — Supplier Payment — AWS (USD 310) | DR | LKR 94,550.00 | LKR 984,407.00 |
| 28 Apr | Payroll — April (Bonus) — note: appears as DR in source | DR | LKR 50,000.00 | LKR 934,407.00 |
| 29 Apr | POS — Keells, Colombo 07 | DR | LKR 6,800.00 | LKR 927,607.00 |
| 30 Apr | Closing Balance | — | — | LKR 927,607.00 |