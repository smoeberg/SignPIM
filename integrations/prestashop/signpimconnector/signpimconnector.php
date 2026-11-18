<?php
/**
 * SignPIM Connector for PrestaShop 8/9 (ADR-018).
 *
 * Sends signed realtime events to SignPIM:
 *   - order.created / order.updated (cancellation) -> SignPIM reserves stock
 *     and lowers it in every connected shop.
 *   - product.updated / stock.updated / product.deleted from the BACK OFFICE
 *     -> SignPIM restores the data it owns.
 *
 * Changes made through the Webservice (i.e. by SignPIM itself) are never sent
 * back, which prevents sync loops.
 *
 * Events are signed with HMAC-SHA256 over "<timestamp>.<body>". Failed
 * deliveries are kept in a local outbox and retried on the next event or by
 * the cron URL shown in the module configuration.
 */
if (!defined('_PS_VERSION_')) {
    exit;
}

class SignpimConnector extends Module
{
    public const CFG_ENDPOINT = 'SIGNPIM_EVENTS_URL';
    public const CFG_SECRET = 'SIGNPIM_EVENTS_SECRET';
    public const CFG_CRON_TOKEN = 'SIGNPIM_CRON_TOKEN';
    private const MAX_ATTEMPTS = 20;

    public function __construct()
    {
        $this->name = 'signpimconnector';
        $this->tab = 'administration';
        $this->version = '1.0.0';
        $this->author = 'SignPIM';
        $this->need_instance = 0;
        $this->ps_versions_compliancy = ['min' => '8.0.0', 'max' => _PS_VERSION_];
        $this->bootstrap = true;
        parent::__construct();
        $this->displayName = 'SignPIM Connector';
        $this->description = 'Realtime orders and back-office changes to SignPIM.';
    }

    public function install()
    {
        Configuration::updateValue(self::CFG_CRON_TOKEN, Tools::passwdGen(32));

        return parent::install()
            && $this->installTable()
            && $this->registerHook('actionValidateOrder')
            && $this->registerHook('actionOrderStatusPostUpdate')
            && $this->registerHook('actionProductUpdate')
            && $this->registerHook('actionProductDelete')
            && $this->registerHook('actionUpdateQuantity');
    }

    public function uninstall()
    {
        Db::getInstance()->execute('DROP TABLE IF EXISTS `' . _DB_PREFIX_ . 'signpim_outbox`');
        foreach ([self::CFG_ENDPOINT, self::CFG_SECRET, self::CFG_CRON_TOKEN] as $key) {
            Configuration::deleteByName($key);
        }

        return parent::uninstall();
    }

    private function installTable()
    {
        return Db::getInstance()->execute('CREATE TABLE IF NOT EXISTS `' . _DB_PREFIX_ . 'signpim_outbox` (
            `id_signpim_outbox` INT UNSIGNED NOT NULL AUTO_INCREMENT,
            `event_id` VARCHAR(96) NOT NULL,
            `body` MEDIUMTEXT NOT NULL,
            `attempts` INT UNSIGNED NOT NULL DEFAULT 0,
            `last_error` VARCHAR(255) NULL,
            `date_add` DATETIME NOT NULL,
            PRIMARY KEY (`id_signpim_outbox`),
            UNIQUE KEY `event_id` (`event_id`)
        ) ENGINE=' . _MYSQL_ENGINE_ . ' DEFAULT CHARSET=utf8mb4');
    }

    // ------------------------------------------------------------------
    // Configuration page
    // ------------------------------------------------------------------
    public function getContent()
    {
        $output = '';
        if (Tools::isSubmit('submitSignpim')) {
            $url = trim((string) Tools::getValue(self::CFG_ENDPOINT));
            if ($url !== '' && strpos($url, 'https://') !== 0) {
                $output .= $this->displayError('The SignPIM events URL must use https.');
            } else {
                Configuration::updateValue(self::CFG_ENDPOINT, $url);
                $secret = (string) Tools::getValue(self::CFG_SECRET);
                if ($secret !== '') {
                    Configuration::updateValue(self::CFG_SECRET, $secret);
                }
                $output .= $this->displayConfirmation('Saved.');
            }
        }
        $cronUrl = $this->context->link->getModuleLink($this->name, 'cron', [
            'token' => Configuration::get(self::CFG_CRON_TOKEN),
        ]);
        $pending = (int) Db::getInstance()->getValue('SELECT COUNT(*) FROM `' . _DB_PREFIX_ . 'signpim_outbox`');

        $form = [
            'form' => [
                'legend' => ['title' => 'SignPIM'],
                'input' => [
                    [
                        'type' => 'text',
                        'label' => 'Events URL',
                        'name' => self::CFG_ENDPOINT,
                        'desc' => 'https://<signpim>/channels/<channel_id>/events',
                    ],
                    [
                        'type' => 'password',
                        'label' => 'Shared secret',
                        'name' => self::CFG_SECRET,
                        'desc' => 'Same value as the env variable referenced by the channel webhook_secret_ref. Leave empty to keep the current secret.',
                    ],
                ],
                'description' => sprintf('Pending undelivered events: %d. Retry cron URL: %s', $pending, $cronUrl),
                'submit' => ['title' => 'Save'],
            ],
        ];
        $helper = new HelperForm();
        $helper->module = $this;
        $helper->name_controller = $this->name;
        $helper->token = Tools::getAdminTokenLite('AdminModules');
        $helper->currentIndex = AdminController::$currentIndex . '&configure=' . $this->name;
        $helper->submit_action = 'submitSignpim';
        $helper->fields_value = [
            self::CFG_ENDPOINT => Configuration::get(self::CFG_ENDPOINT),
            self::CFG_SECRET => '',
        ];

        return $output . $helper->generateForm([$form]);
    }

    // ------------------------------------------------------------------
    // Hooks
    // ------------------------------------------------------------------
    public function hookActionValidateOrder($params)
    {
        if (empty($params['order']) || !Validate::isLoadedObject($params['order'])) {
            return;
        }
        $order = $params['order'];
        $lines = [];
        foreach ($order->getProducts() as $row) {
            $lines[] = [
                'product_id' => (int) $row['product_id'],
                'product_attribute_id' => (int) $row['product_attribute_id'],
                'reference' => (string) $row['product_reference'],
                'quantity' => (int) $row['product_quantity'],
            ];
        }
        $this->send('order-' . (int) $order->id . '-created', 'order.created', [
            'order_id' => (string) $order->id,
            'reference' => (string) $order->reference,
            'lines' => $lines,
        ]);
    }

    public function hookActionOrderStatusPostUpdate($params)
    {
        if (empty($params['newOrderStatus']) || empty($params['id_order'])) {
            return;
        }
        $cancelStates = [
            (int) Configuration::get('PS_OS_CANCELED'),
            (int) Configuration::get('PS_OS_REFUND'),
            (int) Configuration::get('PS_OS_ERROR'),
        ];
        if (!in_array((int) $params['newOrderStatus']->id, $cancelStates, true)) {
            return;
        }
        $orderId = (int) $params['id_order'];
        $this->send('order-' . $orderId . '-cancelled', 'order.updated', [
            'order_id' => (string) $orderId,
            'cancelled' => true,
        ]);
    }

    public function hookActionProductUpdate($params)
    {
        $this->sendBackOfficeProductEvent('product.updated', (int) ($params['id_product'] ?? 0));
    }

    public function hookActionProductDelete($params)
    {
        $this->sendBackOfficeProductEvent('product.deleted', (int) ($params['id_product'] ?? 0));
    }

    public function hookActionUpdateQuantity($params)
    {
        if (!empty($params['id_product_attribute'])) {
            return; // combinations are not managed by SignPIM yet
        }
        $this->sendBackOfficeProductEvent('stock.updated', (int) ($params['id_product'] ?? 0));
    }

    private function sendBackOfficeProductEvent($type, $productId)
    {
        if ($productId <= 0 || !$this->isBackOfficeEdit()) {
            return;
        }
        $this->send(uniqid($type . '-' . $productId . '-', true), $type, ['product_id' => (string) $productId]);
    }

    /**
     * True only for a person editing in the back office. Webservice calls
     * (SignPIM itself), front-office checkout and CLI imports are excluded.
     */
    private function isBackOfficeEdit()
    {
        $script = (string) ($_SERVER['SCRIPT_NAME'] ?? '');
        $uri = (string) ($_SERVER['REQUEST_URI'] ?? '');
        if (strpos($script, '/webservice/') !== false || preg_match('#/api(/|\?|$)#', $uri)) {
            return false;
        }
        if (PHP_SAPI === 'cli') {
            return false;
        }

        return defined('_PS_ADMIN_DIR_')
            && isset($this->context->employee)
            && Validate::isLoadedObject($this->context->employee);
    }

    // ------------------------------------------------------------------
    // Delivery
    // ------------------------------------------------------------------
    private function send($eventId, $type, array $data)
    {
        if (!Configuration::get(self::CFG_ENDPOINT) || !Configuration::get(self::CFG_SECRET)) {
            return;
        }
        $body = json_encode([
            'event_id' => $eventId,
            'type' => $type,
            'data' => $data,
            'shop_url' => Tools::getShopDomainSsl(true),
            'sent_at' => date('c'),
        ], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);

        Db::getInstance()->execute(
            'INSERT IGNORE INTO `' . _DB_PREFIX_ . 'signpim_outbox` (`event_id`, `body`, `date_add`) VALUES ('
            . "'" . pSQL($eventId) . "', '" . pSQL($body, true) . "', NOW())"
        );
        $this->flush(10);
    }

    /**
     * Deliver queued events, oldest first. Returns the number delivered.
     */
    public function flush($limit = 50)
    {
        $rows = Db::getInstance()->executeS(
            'SELECT * FROM `' . _DB_PREFIX_ . 'signpim_outbox` WHERE `attempts` < ' . (int) self::MAX_ATTEMPTS
            . ' ORDER BY `id_signpim_outbox` ASC LIMIT ' . (int) $limit
        );
        $delivered = 0;
        foreach ($rows ?: [] as $row) {
            $error = $this->post((string) $row['body']);
            if ($error === null) {
                Db::getInstance()->delete('signpim_outbox', '`id_signpim_outbox` = ' . (int) $row['id_signpim_outbox']);
                ++$delivered;
                continue;
            }
            Db::getInstance()->execute(
                'UPDATE `' . _DB_PREFIX_ . 'signpim_outbox` SET `attempts` = `attempts` + 1, `last_error` = \''
                . pSQL(Tools::substr($error, 0, 250)) . '\' WHERE `id_signpim_outbox` = ' . (int) $row['id_signpim_outbox']
            );
            break; // keep order: stop at the first failure
        }

        return $delivered;
    }

    /**
     * @return string|null error message, or null on success
     */
    private function post($body)
    {
        $timestamp = (string) time();
        $signature = hash_hmac('sha256', $timestamp . '.' . $body, (string) Configuration::get(self::CFG_SECRET));
        $ch = curl_init((string) Configuration::get(self::CFG_ENDPOINT));
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => 2,
            CURLOPT_TIMEOUT => 4,   // never slow down checkout noticeably
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'X-SignPIM-Timestamp: ' . $timestamp,
                'X-SignPIM-Signature: sha256=' . $signature,
            ],
        ]);
        $response = curl_exec($ch);
        $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $error = curl_error($ch);
        curl_close($ch);
        if ($response === false) {
            return 'curl: ' . $error;
        }

        return ($status >= 200 && $status < 300) ? null : 'HTTP ' . $status . ': ' . Tools::substr((string) $response, 0, 200);
    }
}
