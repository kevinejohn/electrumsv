from collections import defaultdict

from bitcoinx import (
    bip32_key_from_string, be_bytes_to_int, bip32_decompose_chain_string, Address,
)

from electrumsv.app_state import app_state
from electrumsv.device import Device
from electrumsv.exceptions import UserCancelled
from electrumsv.i18n import _
from electrumsv.keystore import Hardware_KeyStore
from electrumsv.logs import logs
from electrumsv.networks import Net
from electrumsv.transaction import classify_tx_output

from ..hw_wallet import HW_PluginBase
from ..hw_wallet.plugin import LibraryFoundButUnusable

logger = logs.get_logger("plugin.trezor")

try:
    import trezorlib
    import trezorlib.transport

    from .client import TrezorClientSV

    from trezorlib.messages import (
        RecoveryDeviceType, HDNodeType, HDNodePathType,
        InputScriptType, OutputScriptType, MultisigRedeemScriptType,
        TxInputType, TxOutputType, TransactionType, SignTx)

    RECOVERY_TYPE_SCRAMBLED_WORDS = RecoveryDeviceType.ScrambledWords
    RECOVERY_TYPE_MATRIX = RecoveryDeviceType.Matrix

    TREZORLIB = True
except Exception as e:
    logger.exception("Failed to import trezorlib")
    TREZORLIB = False

    RECOVERY_TYPE_SCRAMBLED_WORDS, RECOVERY_TYPE_MATRIX = range(2)


# Trezor initialization methods
TIM_NEW, TIM_RECOVER = range(2)

TREZOR_PRODUCT_KEY = 'Trezor'


class TrezorKeyStore(Hardware_KeyStore):
    hw_type = 'trezor'
    device = 'TREZOR'

    def get_derivation(self):
        return self.derivation

    def get_client(self, force_pair=True):
        return self.plugin.get_client(self, force_pair)

    def decrypt_message(self, sequence, message, password):
        raise RuntimeError(_('Encryption and decryption are not implemented by {}').format(
            self.device))

    def sign_message(self, sequence, message, password):
        client = self.get_client()
        address_path = self.get_derivation() + "/%d/%d"%sequence
        address_n = client.expand_path(address_path)
        msg_sig = client.sign_message(self.plugin.get_coin_name(), address_n, message)
        return msg_sig.signature

    def sign_transaction(self, tx, password):
        if tx.is_complete():
            return
        # path of the xpubs that are involved
        xpub_path = {}
        for txin in tx.inputs:
            for x_pubkey in txin.x_pubkeys:
                if not x_pubkey.is_bip32_key():
                    continue
                xpub = x_pubkey.bip32_extended_key()
                if xpub == self.get_master_public_key():
                    xpub_path[xpub] = self.get_derivation()

        self.plugin.sign_transaction(self, tx, xpub_path)


class TrezorPlugin(HW_PluginBase):
    firmware_URL = 'https://wallet.trezor.io'
    libraries_URL = 'https://github.com/trezor/python-trezor'
    minimum_firmware = (1, 5, 2)
    keystore_class = TrezorKeyStore
    minimum_library = (0, 11, 0)
    maximum_library = (0, 12)
    DEVICE_IDS = (TREZOR_PRODUCT_KEY,)

    MAX_LABEL_LEN = 32

    def __init__(self, name):
        super().__init__(name)
        self.logger = logger

        self.libraries_available = self.check_libraries_available()
        if not self.libraries_available:
            return

    def get_library_version(self):
        import trezorlib
        try:
            version = trezorlib.__version__
        except Exception:
            version = 'unknown'
        if TREZORLIB:
            return version
        else:
            raise LibraryFoundButUnusable(library_version=version)

    def enumerate_devices(self):
        devices = trezorlib.transport.enumerate_devices()
        return [Device(path=d.get_path(),
                       interface_number=-1,
                       id_=d.get_path(),
                       product_key=TREZOR_PRODUCT_KEY,
                       usage_page=0,
                       transport_ui_string=d.get_path())
                for d in devices]

    def create_client(self, device, handler):
        try:
            logger.debug("connecting to device at %s", device.path)
            transport = trezorlib.transport.get_transport(device.path)
        except Exception as e:
            logger.error("cannot connect at %s %s", device.path, e)
            return None

        if not transport:
            logger.error("cannot connect at %s", device.path)
            return

        logger.debug("connected to device at %s", device.path)
        # note that this call can still raise!
        return TrezorClientSV(transport, handler, self)

    def get_client(self, keystore, force_pair=True):
        client = app_state.device_manager.client_for_keystore(self, keystore, force_pair)
        # returns the client for a given keystore. can use xpub
        if client:
            client.used()
        return client

    def get_coin_name(self):
        return Net.TREZOR_COIN_NAME

    def initialize_device(self, device_id, wizard, handler):
        # Initialization method
        msg = _("Choose how you want to initialize your {}.\n\n"
                "The first two methods are secure as no secret information "
                "is entered into your computer."
        ).format(self.device, self.device)
        choices = [
            # Must be short as QT doesn't word-wrap radio button text
            (TIM_NEW, _("Let the device generate a completely new seed randomly")),
            (TIM_RECOVER, _("Recover from a seed you have previously written down")),
        ]
        client = app_state.device_manager.client_by_id(device_id)
        model = client.get_trezor_model()
        def f(method):
            import threading
            settings = self.request_trezor_init_settings(wizard, method, model)
            t = threading.Thread(target=self._initialize_device_safe,
                                 args=(settings, method, device_id, wizard, handler))
            t.setDaemon(True)
            t.start()
            exit_code = wizard.loop.exec_()
            if exit_code != 0:
                # this method (initialize_device) was called with the expectation
                # of leaving the device in an initialized state when finishing.
                # signal that this is not the case:
                raise UserCancelled()
        wizard.choice_dialog(title=_('Initialize Device'), message=msg,
                             choices=choices, run_next=f)

    def _initialize_device_safe(self, settings, method, device_id, wizard, handler):
        exit_code = 0
        try:
            self._initialize_device(settings, method, device_id, wizard, handler)
        except UserCancelled:
            exit_code = 1
        except Exception as e:
            self.logger.exception("")
            handler.show_error(str(e))
            exit_code = 1
        finally:
            wizard.loop.exit(exit_code)

    def _initialize_device(self, settings, method, device_id, wizard, handler):
        item, label, pin_protection, passphrase_protection, recovery_type = settings

        if method == TIM_RECOVER and recovery_type == RECOVERY_TYPE_SCRAMBLED_WORDS:
            handler.show_error(_(
                "You will be asked to enter 24 words regardless of your "
                "seed's actual length.  If you enter a word incorrectly or "
                "misspell it, you cannot change it or go back - you will need "
                "to start again from the beginning.\n\nSo please enter "
                "the words carefully!"),
                blocking=True)

        client = app_state.device_manager.client_by_id(device_id)

        if method == TIM_NEW:
            client.reset_device(
                strength=64 * (item + 2),  # 128, 192 or 256
                passphrase_protection=passphrase_protection,
                pin_protection=pin_protection,
                label=label)
        elif method == TIM_RECOVER:
            client.recover_device(
                recovery_type=recovery_type,
                word_count=6 * (item + 2),  # 12, 18 or 24
                passphrase_protection=passphrase_protection,
                pin_protection=pin_protection,
                label=label)
            if recovery_type == RECOVERY_TYPE_MATRIX:
                handler.close_matrix_dialog()
        else:
            raise RuntimeError("Unsupported recovery method")

    def _make_node_path(self, xpub, address_n):
        pubkey = bip32_key_from_string(xpub)
        derivation = pubkey.derivation()
        node = HDNodeType(
            depth=derivation.depth,
            fingerprint=be_bytes_to_int(pubkey.fingerprint()),
            child_num=derivation.n,
            chain_code=derivation.chain_code,
            public_key=pubkey.to_bytes(),
        )
        return HDNodePathType(node=node, address_n=address_n)

    def setup_device(self, device_info, wizard):
        '''Called when creating a new wallet.  Select the device to use.  If
        the device is uninitialized, go through the intialization
        process.'''
        device_id = device_info.device.id_
        client = app_state.device_manager.client_by_id(device_id)
        if client is None:
            raise Exception(_('Failed to create a client for this device.') + '\n' +
                            _('Make sure it is in the correct state.'))
        client.handler = self.create_handler(wizard)
        if not device_info.initialized:
            self.initialize_device(device_id, wizard, client.handler)
        client.get_master_public_key('m')

    def get_master_public_key(self, device_id, derivation, wizard):
        client = app_state.device_manager.client_by_id(device_id)
        client.handler = self.create_handler(wizard)
        return client.get_master_public_key(derivation)

    def get_trezor_input_script_type(self, is_multisig):
        if is_multisig:
            return InputScriptType.SPENDMULTISIG
        else:
            return InputScriptType.SPENDADDRESS

    def sign_transaction(self, keystore, tx, xpub_path):
        client = self.get_client(keystore)
        inputs = self.tx_inputs(tx, xpub_path)
        outputs = self.tx_outputs(keystore.get_derivation(), tx)
        details = SignTx(lock_time=tx.locktime)
        signatures, _ = client.sign_tx(self.get_coin_name(), inputs, outputs, details=details,
                                       prev_txes=defaultdict(TransactionType))
        tx.update_signatures(signatures)

    def show_address(self, wallet, address):
        keystore = wallet.get_keystore()
        client = self.get_client(keystore)
        deriv_suffix = wallet.get_address_index(address)
        derivation = keystore.derivation
        address_path = "%s/%d/%d"%(derivation, *deriv_suffix)

        # prepare multisig, if available:
        xpubs = wallet.get_master_public_keys()
        if len(xpubs) > 1:
            pubkeys = wallet.get_public_keys(address)
            # sort xpubs using the order of pubkeys
            sorted_pairs = sorted(zip(pubkeys, xpubs))
            multisig = self._make_multisig(
                wallet.m,
                [(xpub, deriv_suffix) for _, xpub in sorted_pairs])
        else:
            multisig = None

        script_type = self.get_trezor_input_script_type(multisig is not None)
        client.show_address(address_path, script_type, multisig)

    def tx_inputs(self, tx, xpub_path):
        inputs = []
        for txin in tx.inputs:
            txinputtype = TxInputType()
            txinputtype.prev_hash = bytes(reversed(txin.prev_hash))
            txinputtype.prev_index = txin.prev_idx
            txinputtype.sequence = txin.sequence
            txinputtype.amount = txin.value
            xpubs = [x_pubkey.bip32_extended_key_and_path() for x_pubkey in txin.x_pubkeys]
            txinputtype.multisig = self._make_multisig(txin.threshold,
                xpubs, txin.stripped_signatures_with_blanks())
            txinputtype.script_type = self.get_trezor_input_script_type(
                txinputtype.multisig is not None)
            # find which key is mine
            for xpub, path in xpubs:
                if xpub in xpub_path:
                    xpub_n = bip32_decompose_chain_string(xpub_path[xpub])
                    txinputtype.address_n = xpub_n + path
                    break
            # if txin.script_sig:
            #     txinputtype.script_sig = bytes(txin.script_sig)
            inputs.append(txinputtype)

        return inputs

    def _make_multisig(self, m, xpubs, signatures=None):
        if len(xpubs) == 1:
            return None

        pubkeys = [self._make_node_path(xpub, deriv) for xpub, deriv in xpubs]
        if signatures is None:
            signatures = [b''] * len(pubkeys)
        elif len(signatures) != len(pubkeys):
            raise RuntimeError('Mismatched number of signatures')

        return MultisigRedeemScriptType(
            pubkeys=pubkeys,
            signatures=signatures,
            m=m)

    def tx_outputs(self, derivation, tx):

        def create_output_by_derivation():
            deriv = bip32_decompose_chain_string("m/%d/%d" % index)
            multisig = self._make_multisig(m, [(xpub, deriv) for xpub in xpubs])
            if multisig is None:
                script_type = OutputScriptType.PAYTOADDRESS
            else:
                script_type = OutputScriptType.PAYTOMULTISIG
            return TxOutputType(
                multisig=multisig,
                amount=tx_output.value,
                address_n=bip32_decompose_chain_string(derivation + "/%d/%d" % index),
                script_type=script_type
            )

        def create_output_by_address():
            txoutputtype = TxOutputType()
            txoutputtype.amount = tx_output.value
            address = classify_tx_output(tx_output)
            if isinstance(address, Address):
                txoutputtype.script_type = OutputScriptType.PAYTOADDRESS
                txoutputtype.address = address.to_string()
            return txoutputtype

        outputs = []

        for tx_output, info in zip(tx.outputs, tx.output_info):
            if info:
                # Send derivations of addresses in our wallet
                index, xpubs, m = info
                txoutputtype = create_output_by_derivation()
            else:
                txoutputtype = create_output_by_address()
            outputs.append(txoutputtype)

        return outputs
