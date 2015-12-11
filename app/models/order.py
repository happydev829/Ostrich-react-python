from app import mysql
from app import webapp
from app.models import User
from app.models import Item
from app.models import Utils
from app.models import Wallet
from app.models import Mailer
from app.models import Notifications
from datetime import datetime
import json

class Order():
    def __init__(self, order_id):
        self.order_id = order_id

    def getOrderInfo(self):
        # TODO concatnate list of inv_id and item_id, else this will have
        # multiple rows
        obj_cursor = mysql.connect().cursor()
        obj_cursor.execute("SELECT o.*, oi.* \
                FROM orders o \
                INNER JOIN order_history oi ON o.order_id = oi.order_id \
                WHERE o.order_id = %d" %(self.order_id))
        order_info = Utils.fetchOneAssoc(obj_cursor)
        return order_info

    @staticmethod
    def placeOrder(order_data):
       
        order_fields = ['item_id', 'user_id', 'address']
        for key in order_fields:
            if key not in order_data.keys():
                return {'message': 'Required params missing'}
            elif not order_data[key]:
                return {'message': 'Wrong param value'}
            else:
                order_data[key] = int(order_data[key]) if key != 'address' else json.loads(order_data[key])
        
        order_data['payment_mode'] = Utils.getParam(order_data, 'payment_mode',
                default = 'cash')
        order_data['order_placed'] = Utils.getCurrentTimestamp()
        order_data['order_return'] = Utils.getParam(order_data, 'order_return', 
                default = Utils.getDefaultReturnTimestamp(order_data['order_placed'], 15))
        order_data['delivery_slot'] = int(Utils.getParam(order_data, 'delivery_slot', 
                default = Utils.getDefaultTimeSlot()))

        #TODO calc total amount
        order_data['order_amount'] = 0 

        #check order validity
        # TODO check if item exists

        # User validity
        user = User(order_data['user_id'], 'user_id')
        user_not_valid = Order.isUserValidForOrder(user, order_data)
        if user_not_valid:
            return user_not_valid

        connect = mysql.connect() 
        insert_data_cursor = connect.cursor()
        insert_data_cursor.execute("""INSERT INTO orders (user_id, 
                address_id, 
                order_placed, 
                order_return, 
                delivery_slot, 
                pickup_slot, 
                payment_mode) 
                VALUES(%d, %d, '%s', '%s', %d, %d, '%s')"""  
                %(order_data['user_id'], 
                    order_data['address']['address_id'], 
                    order_data['order_placed'], 
                    order_data['order_return'], 
                    order_data['delivery_slot'], 
                    order_data['delivery_slot'], 
                    order_data['payment_mode']))
        connect.commit()
        order_id = insert_data_cursor.lastrowid
        insert_data_cursor.close()
        response = {'order_id': order_id}

        order = Order(order_id)
        order.updateInventoryPostOrder([order_data['item_id']])

        if order_data['payment_mode'] == 'wallet':
            Wallet.debitTransaction(user.wallet_id, user.user_id, 'order', order_id, order_data['order_amount']) 

        #TODO call roadrunnr api
        #TODO send push notification as a callback to rr response
        order_info = order.getOrderInfo()
        notification_data = {
                    "notification_id": 1,
                    "entity_id": order_info['order_id'],
                    "message": order.getOrderStatusDetails(order_info['order_status'])['Description'] 
                }
        Notifications(user.gcm_id).sendNotification(notification_data)
        return response 
    
    def updateInventoryPostOrder(self, item_ids):
        # NOTE this part is supported for multiple items in same order. PlaceOrder function isnt

        inventory_ids = self.getInventoryIds(item_ids) 

        #update order_history and clear stock in inventory
        connect = mysql.connect()
        for inventory_item in inventory_ids:
            order_history_cursor = connect.cursor()
            order_history_cursor.execute("INSERT INTO order_history (inventory_id, \
                    item_id, order_id) VALUES (%d, %d, %d)" %(inventory_item['inventory_id'], \
                    inventory_item['item_id'], self.order_id))
            connect.commit()
            order_history_cursor.close()

            update_stock_cursor = connect.cursor()
            update_stock_cursor.execute("UPDATE inventory_new SET in_stock = 0 WHERE \
                    inventory_id = %d" % (inventory_item['inventory_id']))
            connect.commit()
            update_stock_cursor.close()


    def getInventoryIds(self, item_ids):
        # Incremental Inventory Logic
        #   in_stock: Item is in inventory (not with a user)
        #   fetched: Item has been acquired
        #     
        #   During order, check if acuquired item is in inventory
        #   If not, insert a new item (to be purchased now), which is not yet
        #   bought(thus, fetched=0). 
        #   fetched=1 happens when 
        #   1.status is changed from the dashboard while entering other 
        #     inventory info (price, isbn)
        #   2. Order status changes to 2(en route).(In case the change was not
        #     made form dashboard.

        inventory_ids = []
        for item_id in item_ids:
            item_check_cursor = mysql.connect().cursor()
            item_check_cursor.execute("""SELECT inventory_id FROM
                    inventory_new WHERE item_id = %d AND in_stock = 1 
                    AND fetched = 1""" % (item_id))
            inv_items = item_check_cursor.fetchall()
            item_check_cursor.close()

            if inv_items: 
                inventory_ids.append({
                    'inventory_id': item_selected[0],
                    'item_id': item_id
                    })
            else:
                connect = mysql.connect()
                insert_inv_item = connect.cursor()
                insert_inv_item.execute("INSERT INTO inventory_new (item_id) VALUES ('%s')" %(item_id))
                connect.commit()
                new_inv_id = insert_inv_item.lastrowid
                insert_inv_item.close()

                inventory_ids.append({
                    'inventory_id': new_inv_id,
                    'item_id': item_id
                    })

        return inventory_ids

    @staticmethod
    def isUserValidForOrder(user, order_data):
        if user.getObj() is None:
            return {'message': 'User does not exist'}

        # User can only own 2 book @ a time
        if webapp.config['USER_BOOKS_LIMIT']:
            if len(user.getCurrentOrders()) >= 2:
                Mailer.excessOrder(user.user_id, order_data['item_id'])
                return {'message': 'Already rented maximum books. We\'ll contact you shortly.'}

        # Wallet validity 
        if order_data['payment_mode'] == 'wallet' and user.wallet_balance is not None and user.wallet_balance < order_data['order_amount']:
            return {'message': 'Not enough balance in wallet'}

        # Since Address is editable before placing order
        if not user.validateUserAddress(order_data['address']):
            return {'message': 'Address not associated'}

        return None


    def getOrderStatusForUser(self, user_id):
        get_status_cursor = mysql.connect().cursor()
        get_status_cursor.execute("SELECT o.order_status, i.item_id FROM orders o \
                INNER JOIN order_history i \
                ON o.order_id = i.order_id \
                WHERE o.order_id = %d \
                AND o.user_id = %d" 
                % (self.order_id, user_id))

        status = get_status_cursor.fetchone()
        if status:
            status_id = int(status[0])
        else:
            return False

        order_info = {}
        if status_id:
            order_info['status_details'] = Order.getOrderStatusDetails(status_id)
            order_info['item'] = Item(int(status[1])).getObj()

        return order_info

    @staticmethod
    def lendItem(lend_data):
        lend_fields = ['item_id', 'user_id', 'address']
        for key in lend_fields:
            if key not in lend_data.keys():
                return {'message': 'Required params missing'}
            elif not lend_data[key]:
                return {'message': 'Wrong param value'}
            else:
                lend_data[key] = int(lend_data[key]) if key != 'address' else json.loads(lend_data[key])

        lend_data['pickup_date'] = Utils.getParam(lend_data, 'pickup_date',
                default=Utils.getCurrentTimestamp())
        lend_data['delivery_date'] = Utils.getParam(lend_data,'delivery_date', 
                default=Utils.getDefaultReturnTimestamp(lend_data['pickup_date'], 45))
        lend_data['pickup_slot'] = int(Utils.getParam(lend_data, 'pickup_slot',
               default = Utils.getDefaultTimeSlot()))
        lend_data['delivery_slot'] = lend_data['pickup_slot']

        # Item conditions is a list of {"name":"condition", "selected": "True/False"}
        item_conditions = Utils.getParam(lend_data, 'item_condition', None)
        item_conditions = json.loads(item_conditions) if item_conditions is not None else item_conditions
        lend_data['item_condition'] = []
        for condition in item_conditions:
            if condition['selected'].lower() == "true":
                lend_data['item_condition'].append(condition['name'])
        lend_data['item_condition'] = "|".join(lend_data['item_condition'])

        # Since Address is editable before placing order
        user = User(lend_data['user_id'], 'user_id')
        if not user.validateUserAddress(lend_data['address']):
            return {'message': 'Address not associated'}

        conn = mysql.connect()
        set_lend_cursor = conn.cursor()
       
        set_lend_cursor.execute("""INSERT INTO inventory_new (item_id, 
                date_added, date_removed, item_condition) VALUES 
                (%d, '%s', '%s', '%s')""" % 
                (lend_data['item_id'], 
                 str(lend_data['pickup_date']), 
                 str(lend_data['delivery_date']), 
                 lend_data['item_condition'] 
                ))
        conn.commit()
        lend_data['inventory_id'] = set_lend_cursor.lastrowid
        set_lend_cursor.close()

        lend_data['lender_id'] = Order.addLender(lend_data) 
        if not lend_data['lender_id']:
            Order.rollbackLend(lend_data['inventory_id'])
            return {}

        # Give 50 credits to lender irrepective of days lent
        user = User(lend_data['user_id'], 'user_id') 
        Wallet.creditTransaction(user.wallet_id, user.user_id, 'lend',
                lend_data['inventory_id'], 50) 

        return {'inventory_id': lend_data['inventory_id'], 'lender_id':
                lend_data['lender_id']}

    @staticmethod    
    def addLender(lend_data):
        conn = mysql.connect()
        lender_cusror = conn.cursor()
        lender_cusror.execute("""INSERT into lenders (
            inventory_id,
            user_id,
            delivery_date,
            pickup_date,
            delivery_slot,
            pickup_slot,
            address_id) VALUES (%d, %d, '%s', '%s', %d, %d, %d) """
            %(lend_data['inventory_id'],
                lend_data['user_id'],
                lend_data['delivery_date'],
                lend_data['pickup_date'],
                lend_data['delivery_slot'],
                lend_data['pickup_slot'],
                lend_data['address']['address_id']))
        conn.commit()
        lender_id = lender_cusror.lastrowid
        lender_cusror.close()
        return lender_id


    @staticmethod    
    def rollbackLend(inventory_id):
        conn = mysql.connect()
        del_cursor = conn.cursor()
        del_cursor.execute("""DELETE FROM inventory WHERE inventory_id = %d"""
                %(inventory_id))
        conn.commit()
        return True
    
    @staticmethod    
    def getTimeSlot():
        time_slot_cursor = mysql.connect().cursor()
        time_slot_cursor.execute("SELECT * FROM time_slots")
        num_slots = time_slot_cursor.rowcount

        time_slots = []
        for slot in range(num_slots):
            time_slots.append(Utils.fetchOneAssoc(time_slot_cursor))

        time_slot_cursor.close()
        return time_slots

    def updateOrderStatus(self, status_id):
        conn = mysql.connect()
        update_cursor = conn.cursor()
        update_cursor.execute("UPDATE orders SET order_status = %d WHERE order_id = %d"
                %(status_id, self.order_id))
        conn.commit()

        update_inv_query = ''
        # Putting item back in stock
        if status_id == 6:
            update_inv_query = """UPDATE inventory_new SET in_stock = 1 WHERE 
                    inventory_id IN (SELECT inventory_id FROM order_history WHERE
                    order_id = %d)""" % (self.order_id)
        elif status_id == 2:
            update_inv_query = """UPDATE inventory_new SET fetched = 1 WHERE
                    inventory_id IN (SELECT inventory_id FROM order_history WHERE
                    order_id = %d)""" % (self.order_id)

        if update_inv_query:
            update_cursor.execute(update_inv_query) 
            conn.commit()
        update_cursor.close()

    def editOrderDetails(self, order_data):
        # order_return validity
        order_info = self.getOrderInfo()
        if not order_info:
            return False

        if 'order_return' in order_data:
            old_order_return = datetime.strptime(order_info['order_return'], "%Y-%m-%d %H:%M:%S")
            new_order_return = datetime.strptime(order_data['order_return'], "%Y-%m-%d %H:%M:%S")
            diff = new_order_return - old_order_return 
            if diff.days <= 0:
                return False
        else:
            order_data['order_return'] = order_info['order_return']
       
        if 'delivery_slot' in order_data:
            if not order_data['delivery_slot'].isdigit():
                return False
            else:
                order_data['delivery_slot'] = int(order_data['delivery_slot'])
                slot_exists = False
                for slot in Order.getTimeSlot():
                    if slot['slot_id'] == order_data['delivery_slot']:
                        slot_exists = True
                        break
                if not slot_exists:
                    return False
        else:
            order_data['delivery_slot'] = order_info['delivery_slot']
       
        # NOTE Future generic incomplete code. Remove else part when using this
        '''
        edit_query_string = []
        for data in order_data.keys():
            if data != 'order_id':
                format_value = "'%s'" if isinstance(order_data[data], 'str') else "%d"
                edit_query_string.append(data+" = "+format_value)
        if not edit_query_string:
            return False
        else:
            edit_query_string ", ".join(edit_query_string)
        '''

        conn = mysql.connect()
        update_cursor = conn.cursor()
        update_cursor.execute("""UPDATE orders SET order_return = '%s',
                delivery_slot = %d WHERE
                order_id = %d""" %(order_data['order_return'],
                order_data['delivery_slot'], self.order_id))
        conn.commit()
        if update_cursor.rowcount:
            return True
        else:
            return False


    @staticmethod
    def getOrderStatusDetails(status_id):
        status_info = {
                1: {
                    "Status": "Order placed",
                    "Description": "Your order has been confirmed"
                    },
                2: {
                    "Status": "Picked up",
                    "Description": "Your order has been picked up for delivery"
                    },
                3: {
                    "Status": "Enroute",
                    "Description": "Your order is on the way"
                    },
                4: {
                    "Status": "Delivered",
                    "Description": "Your order has been delivered"
                    },
                5: {
                    "Status": "Picked up",
                    "Description": "Order has been picked up from the user for return"
                    },
                6: {
                    "Status": "Returned",
                    "Description": "Order has been retured to the inventory"
                    }
                }
        
        if status_id in status_info:
            return status_info[status_id]
        else:
            return False

    
    @staticmethod
    def deleteOrder(order_id):
        conn = mysql.connect()
        delete_cursor = conn.cursor()
        
        order_info = Order(order_id).getOrderInfo()
        if order_info is None:
            return {'status':'false'}

        delete_cursor.execute("""SELECT inventory_id FROM order_history WHERE
        order_id = %d""" %(order_id))
        inventory_id = delete_cursor.fetchone()[0]

        delete_cursor.execute("""DELETE FROM inventory_new WHERE inventory_id =
        """+ str(inventory_id) +""" AND fetched = 0 AND in_stock = 0 AND
        DATE_FORMAT(date_added,'%Y-%m-%d %h:%i') =
        DATE_FORMAT('"""+str(order_info['order_placed'])+"""', '%Y-%m-%d %h:%i')""") 
        conn.commit()
        
        delete_cursor.execute("DELETE orders, order_history FROM orders INNER JOIN \
        order_history WHERE orders.order_id = order_history.order_id AND orders.order_id = %d"
        %(order_id))
        conn.commit()

        delete_cursor.close()
        
        return {'status':'true'}

