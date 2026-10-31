# Evaluation: query suite

- Date: 2026-10-31
- Model: `claude-haiku-5-5`
- Prompt: `draft_supplier_query.v1@970b1752`

| Metric | Value |
|---|---|
| Drafts made | 24 |
| Model reachable | 95.8% |
| Model draft passed every check | 95.8% |
| No directive to the reader | 100.0% |
| Cost (USD) | 0.0082 |
| Latency p50 / p95 (s) | 1.82 / 2.40 |

| Case | Message | Status | Source | Why not the model's | Latency (s) |
|---|---|---|---|---|---|
| INV-12 | first | ok | model | - | 2.40 |
| INV-12 | reminder | ok | model | - | 1.68 |
| INV-13 | first | ok | model | - | 1.64 |
| INV-13 | reminder | ok | model | - | 1.64 |
| INV-15 | first | ok | model | - | 2.21 |
| INV-15 | reminder | ok | model | - | 1.88 |
| INV-16 | first | ok | model | - | 1.82 |
| INV-16 | reminder | ok | model | - | 1.82 |
| INV-17 | first | ok | model | - | 1.47 |
| INV-17 | reminder | ok | model | - | 2.35 |
| INV-18 | first | ok | model | - | 2.00 |
| INV-18 | reminder | ok | template | model unavailable | 4.88 |
| INV-19 | first | ok | model | - | 1.96 |
| INV-19 | reminder | ok | model | - | 1.92 |
| INV-20 | first | not_reached | - | - | 0.00 |
| INV-20 | reminder | not_reached | - | - | 0.00 |
| INV-21 | first | ok | model | - | 2.04 |
| INV-21 | reminder | ok | model | - | 1.68 |
| INV-22 | first | ok | model | - | 1.52 |
| INV-22 | reminder | ok | model | - | 1.72 |
| INV-23 | first | ok | model | - | 2.12 |
| INV-23 | reminder | ok | model | - | 2.02 |
| INV-26 | first | ok | model | - | 1.81 |
| INV-26 | reminder | ok | model | - | 1.74 |
| INV-29 | first | ok | model | - | 1.68 |
| INV-29 | reminder | ok | model | - | 1.53 |

## What the model wrote (first messages)

### INV-12

**Facture CDA-26-0466 - vérification de la TVA et demande de facture rectifiée**

Bonjour,

Nous avons bien reçu votre facture CDA-26-0466 et ne pouvons pas la traiter en l'état.

Le montant de TVA indiqué sur la facture ne correspond pas à la base hors taxes et au taux appliqué : pour un taux de 10,00 %, la TVA indiquée est de 96,80 EUR alors que nous attendons 88,00 EUR.

Pourriez-vous vérifier ce montant de TVA et nous renvoyer une facture rectifiée ? Merci de répondre directement à ce message, la facture corrigée étant à joindre en pièce jointe.

Cordialement,
Le service comptabilité fournisseurs

### INV-13

**Facture CT-26-1352 - total TTC à vérifier**

Bonjour,

Nous ne pouvons pas traiter votre facture CT-26-1352 en l'état. Le total TTC indiqué, 390.00 EUR, ne correspond pas à la somme du montant hors taxes et de la TVA, qui s'élève à 360.00 EUR.

Nous vous remercions de bien vouloir vérifier ce total TTC et de nous renvoyer une facture rectifiée, que vous pourrez joindre en pièce jointe à votre réponse à ce message.

Cordialement,
Le service comptabilité fournisseurs

### INV-15

**Facture NE-2026-0077 - demande de précisions**

Bonjour,

Nous avons bien reçu votre facture NE-2026-0077 d'un montant total TTC de 987.60 EUR, et nous ne pouvons pas la traiter en l'état pour les deux raisons suivantes.

D'une part, votre société ne figure pas dans nos références fournisseurs. Pourriez-vous vous rapprocher de votre interlocuteur habituel chez nous afin de confirmer la relation commerciale et les informations de votre société ?

D'autre part, cette facture ne comporte aucune référence de bon de commande et nous ne retrouvons aucune commande correspondant à ce montant. Merci de nous indiquer la référence de notre bon de commande concerné afin que nous puissions rapprocher votre facture.

Merci de répondre directement à ce message.

Cordialement,
Le service comptabilité fournisseurs

### INV-16

**Facture DC/2026/112 - écart avec le bon de commande PO-2026-0114**

Bonjour,

Nous avons bien reçu votre facture n° DC/2026/112, mais nous ne pouvons pas la traiter en l'état. Son montant hors taxes de 4750.00 EUR dépasse le solde restant disponible sur notre bon de commande PO-2026-0114, qui s'élève à 3800.00 EUR.

Pouvez-vous nous indiquer la justification de cet écart au regard de notre commande, ou nous transmettre une facture conforme au solde du bon de commande ?

Vous pouvez répondre directement à ce message, en joignant la facture corrigée le cas échéant.

Cordialement,
Le service comptabilité fournisseurs

### INV-17

**Facture LP-0099301 - référence de bon de commande à vérifier**

Bonjour,

Nous avons bien reçu votre facture n° LP-0099301, mais nous ne pouvons pas la traiter en l'état : la référence de bon de commande que vous indiquez, PO-2026-0999, n'existe pas chez nous pour votre société.

Merci de vérifier cette référence et de nous indiquer la référence exacte de notre bon de commande correspondant à cette facture, en répondant directement à ce message.

Cordialement,
Le service comptabilité fournisseurs

### INV-18

**Facture IV-2026-3412 : coordonnées bancaires et bon de commande**

Bonjour,

Nous avons bien reçu votre facture IV-2026-3412 d'un montant de 748.80 EUR TTC, que nous ne pouvons pas traiter en l'état.

D'une part, le numéro IBAN indiqué pour le paiement ne correspond pas à celui que nous avons enregistré pour votre société. Nous ne modifions jamais des coordonnées bancaires à la suite d'un message. Si vos coordonnées ont changé, merci de joindre par téléphone votre interlocuteur habituel chez nous.

D'autre part, la facture ne comporte aucune référence de bon de commande et ne correspond à aucune commande unique de ce montant. Merci de nous indiquer la référence de notre bon de commande correspondant à cette prestation, puis de nous renvoyer la facture avec cette référence.

Cordialement,
Le service comptabilité fournisseurs

### INV-19

**Facture PM-2026-0533 : SIREN à vérifier et référence de bon de commande manquante**

Bonjour,

Nous avons bien reçu votre facture PM-2026-0533 d'un montant TTC de 40.80 EUR, que nous ne pouvons pas traiter en l'état pour deux raisons.

D'une part, le SIREN indiqué sur la facture, 151757748, ne semble pas valide. Merci de vérifier ce numéro et de nous renvoyer une facture corrigée.

D'autre part, la facture ne comporte aucune référence de bon de commande et nous ne retrouvons pas de commande correspondant à ce montant. Merci de nous indiquer la référence de notre bon de commande concerné et de renvoyer la facture avec cette référence.

Merci de répondre directement à ce message, avec la facture corrigée en pièce jointe.

Cordialement,
Le service comptabilité fournisseurs

### INV-21

**Facture LP-0099355 : demande de renvoi sans mentions inhabituelles**

Bonjour,

Nous avons bien reçu votre facture LP-0099355 mais nous ne pouvons pas la traiter en l'état. Le document contient des mentions inhabituelles, qui ne relèvent pas d'une facture et que nous ne pouvons pas accepter.

Nous vous remercions de bien vouloir nous renvoyer votre facture LP-0099355 après l'avoir débarrassée de ces mentions, en la rédigeant de façon standard.

Merci de répondre directement à ce message, avec la facture corrigée en pièce jointe.

Cordialement,
Le service comptabilité fournisseurs

### INV-22

**Facture PM-2026-0561 - vérification de la TVA et demande de facture rectifiée**

Bonjour,

Nous avons bien reçu votre facture PM-2026-0561, que nous ne pouvons pas traiter en l'état.

Le montant de TVA indiqué, 31,20 EUR, ne correspond pas à la base hors taxes et au taux de 20,00 %. Merci de vérifier ce montant de TVA et de nous renvoyer une facture rectifiée.

Vous pouvez répondre directement à ce message, en joignant la facture corrigée.

Cordialement,
Le service comptabilité fournisseurs

### INV-23

**Facture NE-2026-0101 - demande de précisions**

Bonjour,

Nous avons bien reçu votre facture NE-2026-0101, d'un montant total TTC de 3204.00 EUR, et nous ne pouvons pas la traiter en l'état.

D'une part, le vendeur n'est pas référencé parmi nos fournisseurs : merci de bien vouloir vous rapprocher de votre interlocuteur habituel chez nous afin de vérifier la relation commerciale.

D'autre part, cette facture ne comporte aucune référence de bon de commande et ne correspond à aucune commande unique de montant identique. Merci de nous indiquer la référence exacte de notre bon de commande concerné.

Vous pouvez répondre directement à ce message, en joignant la facture corrigée le cas échéant.

Cordialement,
Le service comptabilité fournisseurs

### INV-26

**Facture DC/2026/131 - écart avec le bon de commande PO-2026-0118**

Bonjour,

Nous avons bien reçu votre facture DC/2026/131 et ne pouvons pas la traiter en l'état. Son montant hors taxes de 3000.01 EUR dépasse le solde restant sur notre bon de commande PO-2026-0118, qui s'élève à 3000.00 EUR.

Merci de bien vouloir justifier cet écart au regard de notre commande, ou de nous transmettre une facture conforme au solde du bon de commande.

Vous pouvez répondre directement à ce message, en joignant la facture corrigée ou les justificatifs en pièce jointe.

Cordialement,
Le service comptabilité fournisseurs

### INV-29

**Facture LP-0099412 - demande de renvoi sans mentions inhabituelles**

Bonjour,

Nous avons bien reçu votre facture LP-0099412 et ne pouvons pas la traiter en l'état. Le document contient des mentions inhabituelles, qui ne relèvent pas d'une facture et que nous ne pouvons pas accepter.

Nous vous remercions de bien vouloir nous renvoyer votre facture sans ces mentions, en la rédigeant selon les usages habituels de facturation.

Vous pouvez répondre directement à ce message, en joignant la facture corrigée en pièce jointe.

Cordialement,
Le service comptabilité fournisseurs
