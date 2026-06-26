# Script Extract — Verbatim Wording

## Greeting
> "Good day, and welcome to Bank of Sri Lanka. You're speaking with our virtual assistant. What can I assist you with today?"

## Step 1 — Customer Intention
**Service paths:** Account Balance, Block Debit Card, Account Details
**Account number prompt:**
> "Of course. Could you please state the account number you'd like me to look into?"
**Account type clarification prompt:**
> "And just to confirm — is this a Personal or Current account, or is it a Business account?"

## Step 2 — Voice Verification
### Q1 NIC
- Personal: > "To get started, could you please say your National Identity Card number?"
- Business addendum: > Business: Please say the NIC number of the primary account holder.
### Q2 Branch
- Personal: > "Thank you. Which branch was this account originally opened at? Please go ahead and say the branch name."
- Business addendum: > Business: Please say the name of the branch where the business account was registered.
### Q3 DOB
- Personal: > "Could you please say your date of birth — the day, month, and year?"
- Business addendum: > Business: Please say the date of birth of the primary account holder.
### Q4 Mother's Maiden Name
- Personal: > "And finally, could you please say your mother's maiden name?"
- Business addendum: > Business: Please say the maiden name of the primary account holder's mother.

## Step 3 — Identity Confirmation
### Failure (with attempts remaining)
> "I'm sorry, I wasn't able to match that information. You have [X] attempt(s) remaining — please try once more."
### After 3 failed attempts
> "I'm afraid I'm unable to verify your identity at this stage. For your security, I'll transfer you to one of our team members who can assist you further. Please hold."
### Success
> "Thank you for that. I've confirmed your identity successfully. Give me just a moment while I retrieve that for you."

## Step 4 — Serve Request
### Account Balance
- Personal/Current: > "The available balance on your account ending in [XXXX] is [amount]."
- Business: > "The current balance on your business account ending in [XXXX] stands at [amount]."
### Block Debit Card
- Personal/Current: > "I'm placing a block on the debit card associated with account [XXXX] now. That's done — your card has been successfully blocked and you're fully protected."
- Business: > "I'm blocking the debit card linked to business account [XXXX] right away. That's confirmed — your corporate debit card has been blocked successfully."
### Account Details
- Personal/Current: > "Here are the details for account [XXXX]: it's a [type] account, opened on [date] at our [branch] branch. Is there anything else I can help you with today?"
- Business: > "Here are the details for business account [XXXX]: registered under [company name], opened on [date]. Is there anything else I can help you with today?"

## Step 5 — Wrap-Up
- Loop trigger: If the caller requires further assistance → return to Step 1 and capture the new intention.
- Final goodbye: > "It was a pleasure assisting you today, [Name / Company Name]. On behalf of Bank of Sri Lanka, we wish you a wonderful day. Goodbye!"

## Notes Section
- Note: All verification responses are spoken — no keypad entry is required at any stage. The account number is collected at Step 1 alongside the caller's stated intention and is not part of the verification sequence.
