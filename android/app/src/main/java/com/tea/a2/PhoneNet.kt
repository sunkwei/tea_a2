package com.tea.a2

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * The phone's addresses on the local network.
 *
 * The agent listens on every interface (0.0.0.0), so a computer on the same
 * Wi-Fi can open it - but it has to know the address. The app shows it, and
 * the bridge reports it in /phone/device.
 *
 * The phone usually has more than one address: the Wi-Fi (wlan0), a VPN
 * (tun0, the Clash one here) and the cellular ones. Only the Wi-Fi address
 * is the one a computer on the same network can use, so it comes first.
 */
object PhoneNet {

    /** The IPv4 addresses of this phone, the useful one first. */
    fun lanAddresses(context: Context): List<String> {
        val found = LinkedHashSet<String>()

        // 1) the Wi-Fi interface (wlan0 on Android): the address a computer
        //    on the same network uses.
        try {
            val interfaces = NetworkInterface.getNetworkInterfaces()
            while (interfaces != null && interfaces.hasMoreElements()) {
                val item = interfaces.nextElement()
                if (!item.name.startsWith("wlan")) continue
                for (address in item.inetAddresses) {
                    if (address is Inet4Address && !address.isLoopbackAddress) {
                        address.hostAddress?.let { found.add(it) }
                    }
                }
            }
        } catch (e: Exception) {
            // the list is a nicety, not a requirement
        }

        // 2) the Wi-Fi network, the system's own answer. Every network is
        //    handled on its own: one of them (the VPN) can throw.
        try {
            val manager = context.getSystemService(ConnectivityManager::class.java)
            for (network in manager?.allNetworks ?: emptyArray()) {
                try {
                    val capabilities = manager.getNetworkCapabilities(network) ?: continue
                    if (!capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue
                    val properties = manager.getLinkProperties(network) ?: continue
                    for (link in properties.linkAddresses) {
                        val address = link.address
                        if (address is Inet4Address && !address.isLoopbackAddress) {
                            address.hostAddress?.let { found.add(it) }
                        }
                    }
                } catch (e: Exception) {
                    // this network is not ours to look at - the next one may be
                }
            }
        } catch (e: Exception) {
            // no permission or no connectivity manager: the rest still helps
        }

        // 3) the active network (it may be the VPN - that one is last).
        try {
            val manager = context.getSystemService(ConnectivityManager::class.java)
            val properties = manager?.getLinkProperties(manager?.activeNetwork)
            properties?.linkAddresses?.forEach { link ->
                val address = link.address
                if (address is Inet4Address && !address.isLoopbackAddress) {
                    address.hostAddress?.let { found.add(it) }
                }
            }
        } catch (e: Exception) {
            // nothing to add
        }

        // 4) everything else the phone has (a hotspot, the cellular ones).
        try {
            val interfaces = NetworkInterface.getNetworkInterfaces()
            while (interfaces != null && interfaces.hasMoreElements()) {
                val item = interfaces.nextElement()
                if (item.name.startsWith("wlan")) continue
                for (address in item.inetAddresses) {
                    if (address is Inet4Address && !address.isLoopbackAddress) {
                        address.hostAddress?.let { found.add(it) }
                    }
                }
            }
        } catch (e: Exception) {
            // the list is a nicety, not a requirement
        }
        return found.toList()
    }

    /** The address a computer should use, or an empty string. */
    fun primaryAddress(context: Context): String = lanAddresses(context).firstOrNull() ?: ""
}
