from os import environ
import requests
import urllib
import hmac
import pprint
import time

from flask import *

from ruqqus.classes import *
from ruqqus.helpers.wrappers import *
from ruqqus.helpers.security import *
from ruqqus.helpers.alerts import send_notification
from ruqqus.helpers.base36 import *
from ruqqus.__main__ import app

CLIENT=PayPalClient()

def coins_to_price_cents(n):

    if n>=52:
        return 100*n - 1001
    elif n>=26:
        return 100*n-401
    elif n>=12:
        return 100*n-101
    elif n >=4:
        return 100*n-1
    else:
        return 100*n+49

@app.route("/shop/get_price", methods=["GET"])
def shop_get_price():

    coins=int(request.args.get("coins"))

    return jsonify({"price":coins_to_price_cents(coins)/100})

@app.route("/shop/coin_balance", methods=["GET"])
@auth_required
def shop_coin_balance(v):
    return jsonify({"balance":v.coin_balance})


@app.route("/shop/buy_coins", methods=["POST"])
@is_not_banned
@no_negative_balance("html")
@validate_formkey
def shop_buy_coins(v):

    coin_count=int(request.form.get("coin_count",1))

    new_txn=PayPalTxn(
        user_id=v.id,
        created_utc=int(time.time()),
        coin_count=coin_count,
        usd_cents=coins_to_price_cents(coin_count)
        )

    g.db.add(new_txn)
    g.db.flush()

    CLIENT.create(new_txn)

    g.db.add(new_txn)
    g.db.commit()

    return redirect(new_txn.approve_url)


@app.route("/shop/negative_balance", methods=["POST"])
@is_not_banned
@validate_formkey
def shop_negative_balance(v):

    new_txn=PayPalTxn(
        user_id=v.id,
        created_utc=int(time.time()),
        coin_count=0,
        usd_cents=v.negative_balance_cents
        )

    g.db.add(new_txn)
    g.db.flush()

    CLIENT.create(new_txn)

    g.db.add(new_txn)
    g.db.commit()

    return redirect(new_txn.approve_url)

@app.route("/shop/buy_coins_completed", methods=["GET"])
@is_not_banned
def shop_buy_coins_completed(v):

    #look up the txn
    id=request.args.get("txid")
    if not id:
        abort(400)
    id=base36decode(id)
    txn=g.db.query(PayPalTxn).filter_by(user_id=v.id, id=id, status=1).first()

    if not txn:
        abort(400)

    if not CLIENT.capture(txn):
        abort(402)

    g.db.add(txn)
    g.db.flush()

    #successful payment - award coins

    if txn.coin_count:
        v.coin_balance += txn.coin_count
    else:
        v.negative_balance_cents -= txn.usd_cents

    g.db.add(v)

    return redirect("/settings/premium?msg=success")

@app.route("/shop/paypal_webhook")
def paypal_webhook_handler():
    
    #Verify paypal signature
    data={
        "auth_algo":request.headers.get("PAYPAL-AUTH-ALGO"),
        "cert_url":request.headers.get("PAYPAL-CERT-URL"),
        "transmission_id":request.headers.get("PAYPAL-TRANSMISSION-ID"),
        "transmission_sig":request.headers.get("PAYPAL-TRANSMISSION-SIG"),
        "transmission_time":request.headers.get("PAYPAL-TRANSMISSION-TIME"),
        "webhook_id":CLIENT.webhook_id,
        "webhook_event":request.json()
        }


    x=CLIENT._post("/v1/notifications/verify-webhook-signature", json=data)

    if x.json().get("verification_status") != "SUCCESS":
        abort(403)
    
    data=request.json()

    #Reversals
    if data["event_type"] in ["PAYMENT.SALE.REVERSED", "PAYMENT.SALE.REFUNDED"]:

        txn=get_txn(data["resource"]["id"])

        amount_cents=int(float(data["resource"]["amount"]["total"])*100)

    elif data["event_type"] in ["PAYMENT.CAPTURE.REVERSED", "PAYMENT.CAPTURE.REFUNDED"]:

        txn=get_txn(data["resource"]["id"])

        amount_cents=int(float(data["resource"]["amount"]["value"])*100)

    txn.user.negative_balance_cents+=amount_cents

    txn.status=-2

    g.db.add(txn)
    g.db.add(txn.user)

    g.db.flush()



    return "", 204


@app.route("/gift_post/<pid>", methods=["POST"])
@is_not_banned
@no_negative_balance("toast")
@validate_formkey
def gift_post_pid(pid, v):

    post=get_post(pid, v=v)

    if post.author_id==v.id:
        return jsonify({"error":"You can't give awards to yourself."}), 403   

    if post.is_deleted:
        return jsonify({"error":"You can't give awards to deleted posts"}), 403

    if post.is_banned:
        return jsonify({"error":"You can't give awards to removed posts"}), 403

    if post.author.is_deleted:
        return jsonify({"error":"You can't give awards to deleted accounts"}), 403

    if post.author.is_banned and not post.author.unban_utc:
        return jsonify({"error":"You can't give awards to banned accounts"}), 403

    u=get_user(post.author.username, v=v)

    if u.is_blocking:
        return jsonify({"error":"You can't give awards to someone you're blocking."}), 403

    if u.is_blocked:
        return jsonify({"error":"You can't give awards to someone that's blocking you."}), 403

    coins=int(request.args.get("coins",1))

    if not coins:
        return jsonify({"error":"You need to actually give coins."}), 400

    if not v.coin_balance>=coins:
        return jsonify({"error":"You don't have that many coins to give!"}), 403

    v.coin_balance -= coins

    u.coin_balance += coins

    g.db.add(v)
    g.db.add(u)

    if not g.db.query(AwardRelationship).filter_by(user_id=v.id, submission_id=post.id).first():
        send_notification(u, f"Someone liked [your post]({post.permalink}) and has given you a Coin!")

    g.db.commit()

    #create record - uniqueness constraints prevent duplicate award counting
    new_rel = AwardRelationship(
        user_id=v.id,
        submission_id=post.id
        )
    try:
        g.db.add(new_rel)
        g.db.flush()
    except:
        pass

    return jsonify({"message":"Tip Successful!"})

@app.route("/gift_comment/<cid>", methods=["POST"])
@is_not_banned
@no_negative_balance("toast")
@validate_formkey
def gift_comment_pid(cid, v):

    comment=get_comment(cid, v=v)

    if comment.author_id==v.id:
        return jsonify({"error":"You can't give awards to yourself."}), 403      

    if comment.is_deleted:
        return jsonify({"error":"You can't give awards to deleted posts"}), 403

    if comment.is_banned:
        return jsonify({"error":"You can't give awards to removed posts"}), 403

    if comment.author.is_deleted:
        return jsonify({"error":"You can't give awards to deleted accounts"}), 403

    if comment.author.is_banned and not comment.author.unban_utc:
        return jsonify({"error":"You can't give awards to banned accounts"}), 403

    u=get_user(comment.author.username, v=v)

    if u.is_blocking:
        return jsonify({"error":"You can't give awards to someone you're blocking."}), 403

    if u.is_blocked:
        return jsonify({"error":"You can't give awards to someone that's blocking you."}), 403

    coins=int(request.args.get("coins",1))

    if not coins:
        return jsonify({"error":"You need to actually give coins."}), 400

    if not v.coin_balance>=coins:
        return jsonify({"error":"You don't have that many coins to give!"}), 403

    v.coin_balance -= coins

    u.coin_balance += coins

    g.db.add(v)
    g.db.add(u)

    if not g.db.query(AwardRelationship).filter_by(user_id=v.id, comment_id=comment.id).first():
        send_notification(u, f"Someone liked [your comment]({comment.permalink}) and has given you a Coin!")

    g.db.commit()

    #create record - uniqe prevents duplicates
    new_rel = AwardRelationship(
        user_id=v.id,
        comment_id=comment.id
        )
    try:
        g.db.add(new_rel)
        g.db.flush()
    except:
        pass

    return jsonify({"message":"Tip Successful!"})
