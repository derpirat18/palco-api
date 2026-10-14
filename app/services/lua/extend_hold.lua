-- Extension del apartado al entrar al checkout.
--
-- Cubre la ventana peligrosa: el comprador tarda minutos pagando y el
-- TTL original podria vencer a media transaccion. Solo extiende si TODAS
-- las butacas siguen siendo suyas.
--
-- KEYS: llaves de las butacas
-- ARGV[1]: hold_id
-- ARGV[2]: nuevo TTL en segundos
--
-- Devuelve 1 si extendio todas, 0 si perdio alguna.

for i = 1, #KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[1] then
    return 0
  end
end

for i = 1, #KEYS do
  redis.call('EXPIRE', KEYS[i], ARGV[2])
end

return 1
