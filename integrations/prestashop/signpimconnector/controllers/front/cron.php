<?php
/**
 * Retries undelivered SignPIM events. Call every minute from cron:
 *   curl -s "https://shop.example/module/signpimconnector/cron?token=..."
 */
class SignpimconnectorCronModuleFrontController extends ModuleFrontController
{
    public function initContent()
    {
        $token = (string) Tools::getValue('token');
        $expected = (string) Configuration::get(SignpimConnector::CFG_CRON_TOKEN);
        header('Content-Type: application/json');
        if ($expected === '' || !hash_equals($expected, $token)) {
            http_response_code(403);
            exit(json_encode(['error' => 'forbidden']));
        }
        exit(json_encode(['delivered' => $this->module->flush(200)]));
    }
}
