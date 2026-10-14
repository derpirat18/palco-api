-- Apartado atomico de N butacas: todo o nada.
--
-- Redis es monohilo y ejecuta este script completo sin intercalar otros
-- comandos. Por eso mil peticiones simultaneas por la misma butaca se
-- resuelven sin carreras: la primera gana, las demas reciben el indice
-- de la butaca que fallo.
--
-- KEYS: una llave por butaca -> hold:{func:<id>}:<seat_id>
--       El hash tag {func:<id>} fuerza que todas caigan en el mismo slot
--       de Redis Cluster. Sin eso el script falla al escalar.
-- ARGV[1]: hold_id del solicitante
-- ARGV[2]: TTL en segundos
--
-- Devuelve 0 si aparto todas, o el indice (base 1) de la primera ocupada.

for i = 1, #KEYS do
  if redis.call('GET', KEYS[i]) then
    return i
  end
end

for i = 1, #KEYS do
  redis.call('SET', KEYS[i], ARGV[1], 'EX', ARGV[2])
end

return 0
