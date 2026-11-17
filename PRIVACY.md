# Privacy Policy for REMI Discord Bot

**Last Updated: November 18, 2026**

Thank you for using **REMI** ("the Bot", "we", "us", or "our"). This Privacy Policy explains how we collect, use, store, and protect your information when you interact with the Bot on Discord.

By inviting or using REMI, you agree to the collection and use of information in accordance with this Privacy Policy. If you do not agree, please do not use the Bot.

---

## 1. Information We Collect

REMI only collects the minimum amount of data necessary to provide conversational AI functionality:

1. **Discord User ID**: A unique numeric identifier provided by Discord, used to associate your conversation history with your user account.
2. **Interaction Message Content**: Messages that you explicitly send to REMI (such as messages mentioning the bot, replies to the bot's messages, or commands like `!ask`).
3. **Message Timestamps and Roles**: Information identifying whether a message was sent by the user or the bot, and when it was recorded, to maintain chronological conversation flow.

### Information We DO NOT Collect:
- Messages in channels where the bot is not mentioned or addressed.
- Personal Identifiable Information (PII) such as real names, email addresses, phone numbers, or physical addresses.
- Passwords, financial information, payment details, or voice call audio.

---

## 2. How We Use Your Information

The data collected is used solely for the following purposes:
- **Conversation Context**: Maintaining context for multi-turn conversations with the AI (up to the last 20 messages per user).
- **Generating Responses**: Sending the prompt and recent conversation history to the Google Gemini API to produce relevant, contextual replies.
- **Service Stability**: Ensuring the bot functions reliably and debugging errors if they occur.

We **never** sell, trade, rent, or monetize your data. We do not use your data for advertising, profiling, or automated decision-making outside the direct conversational interaction.

---

## 3. Data Storage, Retention, and Security

- **Storage**: Data is stored securely in a managed PostgreSQL database.
- **Retention Limit**: We implement a strict rolling retention policy: only the **20 most recent messages** per user are retained. Older messages are automatically and permanently deleted from our database.
- **Security**: Access to the database is restricted to authenticated services with encrypted connections. We implement reasonable administrative and technical safeguards to protect your data.

---

## 4. Third-Party Services

To provide AI functionality, REMI interacts with:
- **Discord API** (Discord Inc.): To receive and send messages on the Discord platform. Discord's Privacy Policy can be found at [https://discord.com/privacy](https://discord.com/privacy).
- **Google Gemini API** (Google LLC): To process prompts and generate intelligent responses. Data transmitted to Google is handled in accordance with Google's API Terms of Service and Privacy Policy.

---

## 5. User Rights and Data Deletion

You have the right to access, inspect, or delete your stored data at any time:

1. **Self-Service Deletion**: You can purge your entire conversation history at any time by running the `!clear` (or `!cleardata`) command in any channel or direct message where the bot is present.
2. **Manual Request**: You may contact the bot developer directly on Discord or through GitHub to request complete erasure of any data associated with your Discord User ID. Upon receiving a valid request, your data will be permanently removed within 48 hours.

---

## 6. Children's Privacy

REMI does not knowingly collect or solicit personal information from anyone under the age required by Discord's Terms of Service (13 years old, or the minimum legal age in your country). If you believe a child under this age has used the bot and provided personal information, please contact us immediately to have the data removed.

---

## 7. Changes to This Privacy Policy

We may update this Privacy Policy from time to time. Any changes will be reflected with an updated "Last Updated" date at the top of this document. Continued use of the Bot after changes take effect constitutes acceptance of the revised policy.

---

## 8. Contact Information

If you have questions, feedback, or requests regarding this Privacy Policy or your data, please contact:
- **GitHub**: [https://github.com/just-kett/REMI](https://github.com/just-kett/REMI)
- **Developer Contact**: Via Discord or GitHub Issues
