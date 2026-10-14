-- Liberacion segura de butacas apartadas.
--
-- Nunca borra a ciegas. Si el TTL ya expiro y otra persona aparto la
-- butaca, un DEL directo destruiria el apartado ajeno. Por eso compara
-- el dueño antes de borrar.
--
-- KEYS: llaves de las butacas
-- ARGV[1]: hold_id que dice ser el dueño
--
-- Devuelve cuantas libero efectivamente.

local liberadas = 0
for i = 1, #KEYS do
  if redis.call('GET', KEYS[i]) == ARGV[1] then
    redis.call('DEL', KEYS[i])
    liberadas = liberadas + 1
  end
end

return liberadas
